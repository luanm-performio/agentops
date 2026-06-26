import logging
from typing import cast

from django.conf import settings
from django.db.models import QuerySet
from django_q.tasks import async_task

from .base import CommandDefinition, CommandParams, positive_int, required_str
from ..models import LockMonitorCapture, LockMonitoringRecord

logger = logging.getLogger(__name__)

ALERT_THRESHOLD_MAX_SECONDS = 24 * 60 * 60
ALERT_RECORD_LIMIT = 5


def run(params: CommandParams) -> str:
    tenant_host = required_str(params, "tenant_host")
    long_running_seconds = positive_int(
        params,
        "long_running_seconds",
        maximum=300,
    )

    from ..lock_monitor import run_lock_monitor  # noqa: PLC0415

    capture = run_lock_monitor(
        tenant_host,
        long_running_seconds=long_running_seconds,
        command_run_id=cast(int | None, params.get("_command_run_id")),
    )
    alert_status = _queue_alert_if_needed(capture, params)
    alert_suffix = f" {alert_status}" if alert_status else ""
    return (
        f"Lock monitor capture #{capture.pk} completed for {tenant_host}: "
        f"{capture.data_source_count} data source(s), "
        f"{capture.process_count} process(es), {capture.lock_count} lock wait(s)."
        f"{alert_suffix}"
    )


COMMAND = CommandDefinition(
    key="lock_monitor",
    label="Lock Monitor",
    description=(
        "Resolve a tenant host, capture full running queries and InnoDB lock "
        "waits, and save the snapshot for review."
    ),
    default_params={
        "tenant_host": "",
        "long_running_seconds": 300,
        "alert_threshold_seconds": 7200,
        "alert_agent_id": "",
        "alert_agent_name": "",
        "alert_recipient": "me",
    },
    handler=run,
)


def _queue_alert_if_needed(
    capture: LockMonitorCapture,
    params: CommandParams,
) -> str:
    threshold_seconds = _optional_positive_int(
        params,
        "alert_threshold_seconds",
        maximum=ALERT_THRESHOLD_MAX_SECONDS,
    )
    if threshold_seconds is None:
        return ""

    overdue_records = _overdue_records(capture, threshold_seconds)
    if not overdue_records.exists():
        return ""

    agent_pk = _alert_agent_pk(params)
    if agent_pk is None:
        return (
            "Alert skipped: configure alert_agent_id, alert_agent_name, "
            "or LOCK_MONITOR_ALERT_AGENT_ID."
        )

    overdue_count = overdue_records.count()
    prompt = _alert_prompt(
        capture,
        list(overdue_records[:ALERT_RECORD_LIMIT]),
        threshold_seconds,
        recipient=_alert_recipient(params),
        overdue_count=overdue_count,
    )
    try:
        async_task("agents.tasks.run_agent_prompt", agent_pk, prompt)
    except Exception as exc:
        logger.exception(
            "Failed to queue lock monitor alert (capture %s, agent %s)",
            capture.pk,
            agent_pk,
        )
        return f"Alert failed to queue: {exc}"
    return f"Alert queued for {overdue_count} overdue job(s)."


def _overdue_records(
    capture: LockMonitorCapture,
    threshold_seconds: int,
) -> QuerySet[LockMonitoringRecord]:
    return capture.records.filter(
        record_type=LockMonitoringRecord.PROCESS,
        duration_seconds__gte=threshold_seconds,
    ).order_by("-duration_seconds", "schema_name", "process_id")


def _alert_agent_pk(params: CommandParams) -> int | None:
    raw_agent_id = _setting_or_param(
        params,
        "alert_agent_id",
        "LOCK_MONITOR_ALERT_AGENT_ID",
    )
    if raw_agent_id:
        try:
            return int(raw_agent_id)
        except ValueError:
            logger.warning("Invalid lock monitor alert agent id: %s", raw_agent_id)
            return None

    agent_name = _setting_or_param(
        params,
        "alert_agent_name",
        "LOCK_MONITOR_ALERT_AGENT_NAME",
    )
    if not agent_name:
        return None

    from agents.models import Agent

    agent = Agent.objects.filter(name__iexact=agent_name).first()
    if agent is None:
        logger.warning("Lock monitor alert agent %r was not found", agent_name)
        return None
    return agent.pk


def _alert_recipient(params: CommandParams) -> str:
    return (
        _setting_or_param(
            params,
            "alert_recipient",
            "LOCK_MONITOR_ALERT_RECIPIENT",
        )
        or "me"
    )


def _setting_or_param(
    params: CommandParams,
    param_name: str,
    setting_name: str,
) -> str:
    value = params.get(param_name)
    if value not in (None, ""):
        return str(value).strip()
    return str(getattr(settings, setting_name, "")).strip()


def _alert_prompt(
    capture: LockMonitorCapture,
    records: list[LockMonitoringRecord],
    threshold_seconds: int,
    *,
    recipient: str,
    overdue_count: int,
) -> str:
    threshold_label = _duration_label(threshold_seconds)
    lines = [
        "Send a Slack alert to "
        f"{recipient} that lock monitor capture #{capture.pk} found "
        f"{overdue_count} MySQL job(s) running at least {threshold_label}.",
        "",
        f"Tenant host: {capture.tenant_host}",
        f"Capture started: {capture.started_at.isoformat()}",
        "",
        f"Jobs shown: {len(records)}",
    ]
    for record in records:
        lines.append(
            "- "
            f"{record.schema_name} on {record.database_server_url}: "
            f"process {record.process_id or 'unknown'}, "
            f"duration {_duration_label(record.duration_seconds or 0)}, "
            f"state {record.state or 'unknown'}, "
            f"query {_shorten(record.query_text)}"
        )
    lines.extend(
        [
            "",
            "Keep the Slack message short and action-oriented. Mention that this "
            "came from the lock monitor.",
        ]
    )
    return "\n".join(lines)


def _optional_positive_int(
    params: CommandParams,
    key: str,
    *,
    maximum: int,
) -> int | None:
    value = params.get(key)
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a whole number.") from exc
    if parsed <= 0:
        return None
    if parsed > maximum:
        raise ValueError(f"{key} must not exceed {maximum} seconds.")
    return parsed


def _duration_label(seconds: int) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if seconds >= 60 and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def _shorten(value: str, *, limit: int = 160) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized or "empty"
    return f"{normalized[: limit - 1]}..."
