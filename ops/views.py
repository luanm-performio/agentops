import json
import logging
from datetime import datetime, time

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .command_registry import COMMANDS
from .command_service import CommandScheduleService, enqueue_command_run
from .calc_log import build_calc_log_dashboard
from .forms import (
    CalcLogDashboardForm,
    CommandRunForm,
    CommandScheduleForm,
    LockMonitorRunForm,
)
from .models import (
    CalcLogDashboard,
    CommandRun,
    CommandSchedule,
    LockMonitorCapture,
    LockMonitoringRecord,
)
from .squelch_lookup import (
    SquelchQueryResult,
    build_squelch_xlsx,
    get_squelch_regions,
    run_squelch_query,
)
from .tenant_lookup import TenantLookupResult, locate_tenants

logger = logging.getLogger(__name__)


@login_required
@require_http_methods(["GET", "POST"])
def calc_log_dashboards(request: HttpRequest) -> HttpResponse:
    form = CalcLogDashboardForm(request.POST or None, request.FILES or None)
    status = 200

    if request.method == "POST":
        if form.is_valid():
            try:
                result = build_calc_log_dashboard(form.cleaned_data["log_files"])
            except Exception as exc:
                logger.exception(
                    "Calculation log dashboard generation failed for files=%s",
                    [
                        uploaded_file.name
                        for uploaded_file in form.cleaned_data["log_files"]
                    ],
                )
                form.add_error(None, f"Dashboard generation failed: {exc}")
                status = 500
            else:
                dashboard = form.save(commit=False)
                dashboard.source_folder = ""
                dashboard.source_files = result.source_files
                dashboard.html_content = result.html
                dashboard.save()
                detail_url = reverse("calc_log_dashboard_detail", args=[dashboard.pk])
                if request.headers.get("HX-Request"):
                    response = HttpResponse(status=204)
                    response["HX-Redirect"] = detail_url
                    return response
                return redirect(detail_url)
        else:
            status = 422

    context = _calc_log_dashboard_list_context(request, form)
    if request.headers.get("HX-Request"):
        return render(request, "partials/_calc_log.html", context, status=status)
    return render(request, "calc_log.html", context, status=status)


def _calc_log_dashboard_list_context(
    request: HttpRequest,
    form: CalcLogDashboardForm,
) -> dict[str, object]:
    dashboards = Paginator(CalcLogDashboard.objects.all(), 10).get_page(
        request.GET.get("page")
    )
    return {"form": form, "dashboards": dashboards}


@login_required
@require_GET
def calc_log_dashboard_detail(request: HttpRequest, pk: int) -> HttpResponse:
    dashboard = get_object_or_404(CalcLogDashboard, pk=pk)
    context = {"dashboard": dashboard}
    if request.headers.get("HX-Request"):
        return render(request, "partials/_calc_log_detail.html", context)
    return render(request, "calc_log_detail.html", context)


@login_required
@require_POST
def calc_log_dashboard_delete(request: HttpRequest, pk: int) -> HttpResponse:
    dashboard = get_object_or_404(CalcLogDashboard, pk=pk)
    dashboard.delete()

    if request.headers.get("HX-Request"):
        response = render(
            request,
            "partials/_calc_log.html",
            _calc_log_dashboard_list_context(request, CalcLogDashboardForm()),
        )
        response["HX-Push-Url"] = reverse("calc_log_dashboards")
        return response
    return redirect("calc_log_dashboards")


@login_required
@require_GET
@xframe_options_sameorigin
def calc_log_dashboard_html(request: HttpRequest, pk: int) -> HttpResponse:
    dashboard = get_object_or_404(CalcLogDashboard, pk=pk)
    response = HttpResponse(
        dashboard.html_content, content_type="text/html; charset=utf-8"
    )
    response["Content-Security-Policy"] = (
        "default-src 'none'; "
        "script-src 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src 'unsafe-inline'; "
        "img-src data:; font-src data:; connect-src 'none'; "
        "object-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'self'; "
        "sandbox allow-scripts allow-downloads"
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def lock_monitor(request: HttpRequest) -> HttpResponse:
    queued_run: CommandRun | None = None
    form = LockMonitorRunForm(request.POST or None)
    status = 200

    if request.method == "POST":
        if form.is_valid():
            queued_run = enqueue_command_run(
                "lock_monitor",
                {"tenant_host": form.cleaned_data["tenant_host"]},
            )
            form = LockMonitorRunForm()
        else:
            status = 422

    context = {
        "form": form,
        "queued_run": queued_run,
        **_lock_monitor_context(request),
    }
    if request.headers.get("HX-Request"):
        return render(request, "partials/_lock_monitor.html", context, status=status)
    return render(request, "lock_monitor.html", context, status=status)


@login_required
@require_GET
def lock_monitor_results(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "partials/_lock_monitor_results.html",
        _lock_monitor_context(request),
    )


@login_required
@require_GET
def lock_monitor_export(request: HttpRequest) -> HttpResponse:
    records = _filtered_lock_monitor_records(request)[:10000]
    columns = [
        "run_id",
        "capture_id",
        "captured_at",
        "tenant_host",
        "record_type",
        "region",
        "database_server_url",
        "schema_name",
        "process_id",
        "waiting_process_id",
        "blocking_process_id",
        "user",
        "host",
        "database_name",
        "command",
        "duration_seconds",
        "state",
        "query_text",
        "blocking_query_text",
        "is_tenant_schema",
        "is_long_running",
        "is_lock_waiting",
        "is_lock_blocking",
        "contention_signals",
        "statement_metrics",
        "server_metrics",
        "explanation",
        "explain_error",
        "explain_skipped_reason",
        "explain_json",
        "raw_data",
    ]
    rows = [
        {
            "run_id": record.capture.command_run_id,
            "capture_id": record.capture_id,
            "captured_at": record.captured_at.isoformat(),
            "tenant_host": record.capture.tenant_host,
            "record_type": record.record_type,
            "region": record.region,
            "database_server_url": record.database_server_url,
            "schema_name": record.schema_name,
            "process_id": record.process_id,
            "waiting_process_id": record.waiting_process_id,
            "blocking_process_id": record.blocking_process_id,
            "user": record.user,
            "host": record.host,
            "database_name": record.database_name,
            "command": record.command,
            "duration_seconds": record.duration_seconds,
            "state": record.state,
            "query_text": record.query_text,
            "blocking_query_text": record.blocking_query_text,
            "is_tenant_schema": record.is_tenant_schema,
            "is_long_running": record.is_long_running,
            "is_lock_waiting": record.is_lock_waiting,
            "is_lock_blocking": record.is_lock_blocking,
            "contention_signals": json.dumps(record.contention_signals, indent=2),
            "statement_metrics": json.dumps(record.statement_metrics, indent=2),
            "server_metrics": json.dumps(record.server_metrics, indent=2),
            "explanation": record.explanation,
            "explain_error": record.explain_error,
            "explain_skipped_reason": record.explain_skipped_reason,
            "explain_json": (
                json.dumps(record.explain_json, indent=2)
                if record.explain_json is not None
                else ""
            ),
            "raw_data": json.dumps(record.raw_data, indent=2),
        }
        for record in records
    ]
    result = SquelchQueryResult(columns=columns, rows=rows)
    response = HttpResponse(
        build_squelch_xlsx(result),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="lock-monitor.xlsx"'
    return response


def _lock_monitor_context(
    request: HttpRequest,
    *,
    command_run_id: int | None = None,
) -> dict[str, object]:
    run_id = (
        str(command_run_id)
        if command_run_id is not None
        else request.GET.get("run_id", "").strip()
    )
    tenant_host = request.GET.get("tenant_host", "").strip()
    auto_refresh_enabled = request.GET.get("auto_refresh", "1") != "0"
    captures = LockMonitorCapture.objects.select_related("command_run")
    if run_id:
        captures = (
            captures.filter(command_run_id=int(run_id))
            if run_id.isdigit()
            else captures.none()
        )
    if tenant_host:
        captures = captures.filter(tenant_host__icontains=tenant_host)

    filtered_records = _filtered_lock_monitor_records(request)
    if _has_lock_monitor_record_filters(request):
        captures = captures.filter(pk__in=filtered_records.values("capture_id"))

    capture_page = Paginator(captures, 5).get_page(request.GET.get("page"))
    capture_list = list(capture_page.object_list)
    capture_ids = [capture.pk for capture in capture_list]
    records = list(filtered_records.filter(capture_id__in=capture_ids))
    records_by_capture: dict[int, list[LockMonitoringRecord]] = {
        capture_id: [] for capture_id in capture_ids
    }
    for record in records:
        records_by_capture.setdefault(record.capture_id, []).append(record)

    snapshots = [
        _lock_monitor_snapshot_context(
            capture,
            records_by_capture.get(capture.pk, []),
        )
        for capture in capture_list
    ]
    filter_params = request.GET.copy()
    filter_params.pop("page", None)
    if command_run_id is not None:
        filter_params["run_id"] = run_id
    return {
        "captures": capture_list,
        "capture_page": capture_page,
        "filter_query": filter_params.urlencode(),
        "snapshots": snapshots,
        "latest_snapshot": snapshots[0] if snapshots else None,
        "records": records,
        "record_count": len(records),
        "run_id_filter": run_id,
        "tenant_host_filter": tenant_host,
        "schema_filter": request.GET.get("schema", "").strip(),
        "record_type_filter": request.GET.get("record_type", "").strip(),
        "captured_date_filter": request.GET.get("captured_date", "").strip(),
        "from_time_filter": request.GET.get("from_time", "").strip(),
        "to_time_filter": request.GET.get("to_time", "").strip(),
        "search_filter": request.GET.get("search", "").strip(),
        "auto_refresh_enabled": auto_refresh_enabled,
        "record_types": LockMonitoringRecord.RECORD_TYPES,
    }


def _lock_monitor_snapshot_context(
    capture: LockMonitorCapture,
    records: list[LockMonitoringRecord],
) -> dict[str, object]:
    lock_records = [
        record
        for record in records
        if record.record_type == LockMonitoringRecord.LOCK_WAIT
    ]
    process_records = [
        record
        for record in records
        if record.record_type == LockMonitoringRecord.PROCESS
    ]
    tenant_records = [record for record in process_records if record.is_tenant_schema]
    other_records = [
        record for record in process_records if not record.is_tenant_schema
    ]
    resource_records = [
        record
        for record in records
        if record.record_type == LockMonitoringRecord.RESOURCE
    ]
    tenant_schema = next(
        (record.schema_name for record in records if record.schema_name),
        "tenant schema",
    )

    status = "ok"
    status_label = "All clear"
    if capture.status == LockMonitorCapture.FAILED:
        status = "alert"
        status_label = "Capture failed"
    elif lock_records or capture.lock_count:
        status = "alert"
        status_label = "Lock contention"
    elif capture.warning or any(
        record.is_long_running or record.contention_signals for record in tenant_records
    ):
        status = "warn"
        status_label = "Needs attention"
    elif capture.status == LockMonitorCapture.RUNNING:
        status = "watch"
        status_label = "Capture running"
    elif tenant_records or any(record.is_long_running for record in other_records):
        status = "watch"
        status_label = "Activity observed"

    diagnosis = _lock_monitor_diagnosis(capture, tenant_records, lock_records)
    top_suspects = _lock_monitor_top_suspects(
        [*lock_records, *tenant_records],
        limit=5,
    )

    return {
        "capture": capture,
        "status": status,
        "status_label": status_label,
        "diagnosis": diagnosis,
        "top_suspects": top_suspects,
        "tenant_schema": tenant_schema,
        "lock_records": lock_records,
        "tenant_records": tenant_records,
        "other_records": other_records,
        "resource_records": resource_records,
        "shown_record_count": len(records),
    }


def _lock_monitor_diagnosis(
    capture: LockMonitorCapture,
    tenant_records: list[LockMonitoringRecord],
    lock_records: list[LockMonitoringRecord],
) -> dict[str, object]:
    tenant_signal_counts: dict[str, int] = {}
    for record in tenant_records:
        for signal in record.contention_signals:
            tenant_signal_counts[signal] = tenant_signal_counts.get(signal, 0) + 1

    long_running_count = sum(record.is_long_running for record in tenant_records)
    likely_issue = "No active lock wait found"
    next_step = (
        "If the calculation is slow, inspect long-running queries, repeated query "
        "patterns, or application-side calculation steps."
    )

    if capture.status == LockMonitorCapture.FAILED:
        likely_issue = "Capture failed"
        next_step = "Fix the capture error before diagnosing query runtime."
    elif lock_records or capture.lock_count:
        likely_issue = "Lock contention detected"
        next_step = (
            "Start with the blocking query or sleeping transaction, then check "
            "whether it belongs to this tenant or another schema."
        )
    elif long_running_count:
        likely_issue = "Long-running tenant query without an active lock wait"
        next_step = (
            "Treat this as query/runtime slowness: check EXPLAIN, rows examined, "
            "temp disk tables, no-index signals, and calculation step logs."
        )
    elif tenant_signal_counts:
        likely_issue = "Suspicious query runtime signals"
        next_step = (
            "No lock wait is visible, but statement/server metrics show work that "
            "can slow calculations."
        )
    elif capture.warning:
        likely_issue = "Capture warnings need review"
        next_step = "Some diagnostic sources were unavailable; review the warning text."
    elif capture.status == LockMonitorCapture.RUNNING:
        likely_issue = "Capture still running"
        next_step = "Wait for completion before drawing a conclusion."

    signal_labels = [
        _lock_monitor_signal_label(signal)
        for signal, _count in sorted(
            tenant_signal_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:6]
    ]

    return {
        "likely_issue": likely_issue,
        "next_step": next_step,
        "long_running_count": long_running_count,
        "lock_wait_count": len(lock_records) or capture.lock_count,
        "signal_labels": signal_labels,
    }


def _lock_monitor_top_suspects(
    records: list[LockMonitoringRecord],
    *,
    limit: int,
) -> list[dict[str, object]]:
    scored = [
        (_lock_monitor_record_score(record), record)
        for record in records
        if record.record_type == LockMonitoringRecord.LOCK_WAIT
        or record.is_long_running
        or record.contention_signals
    ]
    scored.sort(
        key=lambda item: (
            -item[0],
            -(item[1].duration_seconds or 0),
            item[1].schema_name,
            item[1].process_id or item[1].waiting_process_id or 0,
        )
    )
    return [
        {
            "record": record,
            "severity": _lock_monitor_record_severity(score),
            "reason": _lock_monitor_record_reason(record),
            "signal_labels": [
                _lock_monitor_signal_label(signal)
                for signal in record.contention_signals[:5]
            ],
        }
        for score, record in scored[:limit]
    ]


def _lock_monitor_record_score(record: LockMonitoringRecord) -> int:
    score = 0
    if record.record_type == LockMonitoringRecord.LOCK_WAIT:
        score += 100
    if record.is_lock_waiting:
        score += 80
    if record.is_lock_blocking:
        score += 70
    if record.is_long_running:
        score += 35
    if (record.duration_seconds or 0) >= 3600:
        score += 20
    if (record.duration_seconds or 0) >= 7200:
        score += 20
    score += min(len(record.contention_signals) * 8, 40)
    return score


def _lock_monitor_record_severity(score: int) -> str:
    if score >= 100:
        return "critical"
    if score >= 55:
        return "warning"
    return "watch"


def _lock_monitor_record_reason(record: LockMonitoringRecord) -> str:
    if record.record_type == LockMonitoringRecord.LOCK_WAIT:
        return "Lock wait captured"
    if record.is_lock_waiting:
        return "Waiting on a lock"
    if record.is_lock_blocking:
        return "Blocking another query"
    if record.is_long_running:
        return "Long-running query"
    if record.contention_signals:
        return "Runtime signals detected"
    return "Observed activity"


def _lock_monitor_signal_label(signal: str) -> str:
    labels = {
        "waiting_on_lock": "Waiting on lock",
        "blocking_other_query": "Blocking other query",
        "sleeping_transaction_holds_lock": "Sleeping transaction holds lock",
        "statement_lock_time": "Statement lock time",
        "statement_temp_disk_tables": "Disk temp table",
        "statement_no_index_used": "No index used",
        "statement_no_good_index": "No good index",
        "statement_sort_merge_passes": "Sort merge passes",
        "high_rows_examined_to_sent": "High rows examined",
        "server_connection_utilization_high": "High connection use",
        "server_innodb_lock_waits_active": "Server lock waits active",
    }
    return labels.get(signal, signal.replace("_", " ").title())


def _filtered_lock_monitor_records(
    request: HttpRequest,
) -> QuerySet[LockMonitoringRecord]:
    records = LockMonitoringRecord.objects.select_related("capture")
    run_id = request.GET.get("run_id", "").strip()
    tenant_host = request.GET.get("tenant_host", "").strip()
    schema = request.GET.get("schema", "").strip()
    record_type = request.GET.get("record_type", "").strip()
    search = request.GET.get("search", "").strip()
    captured_after, captured_before = _lock_monitor_captured_at_bounds(request)

    if run_id:
        records = (
            records.filter(capture__command_run_id=int(run_id))
            if run_id.isdigit()
            else records.none()
        )
    if tenant_host:
        records = records.filter(capture__tenant_host__icontains=tenant_host)
    if schema:
        records = records.filter(schema_name__icontains=schema)
    if record_type in dict(LockMonitoringRecord.RECORD_TYPES):
        records = records.filter(record_type=record_type)
    if captured_after is not None:
        records = records.filter(captured_at__gte=captured_after)
    if captured_before is not None:
        records = records.filter(captured_at__lte=captured_before)
    if search:
        records = records.filter(
            Q(query_text__icontains=search)
            | Q(blocking_query_text__icontains=search)
            | Q(explanation__icontains=search)
            | Q(explain_error__icontains=search)
            | Q(explain_skipped_reason__icontains=search)
            | Q(user__icontains=search)
            | Q(host__icontains=search)
            | Q(state__icontains=search)
        )
    return records


def _has_lock_monitor_record_filters(request: HttpRequest) -> bool:
    captured_after, captured_before = _lock_monitor_captured_at_bounds(request)
    record_type = request.GET.get("record_type", "").strip()
    return bool(
        request.GET.get("schema", "").strip()
        or request.GET.get("search", "").strip()
        or record_type in dict(LockMonitoringRecord.RECORD_TYPES)
        or captured_after is not None
        or captured_before is not None
    )


def _lock_monitor_captured_at_bounds(
    request: HttpRequest,
) -> tuple[datetime | None, datetime | None]:
    captured_date = parse_date(request.GET.get("captured_date", "").strip())
    if captured_date is None:
        return None, None

    from_time = parse_time(request.GET.get("from_time", "").strip()) or time.min
    to_time = parse_time(request.GET.get("to_time", "").strip()) or time.max
    current_timezone = timezone.get_current_timezone()
    return (
        timezone.make_aware(
            datetime.combine(captured_date, from_time), current_timezone
        ),
        timezone.make_aware(datetime.combine(captured_date, to_time), current_timezone),
    )


@login_required
@require_http_methods(["GET", "POST"])
def locate_tenant(request: HttpRequest) -> HttpResponse:
    host_name = ""
    results: list[TenantLookupResult] = []
    error = ""
    has_searched = False
    status = 200

    if request.method == "POST":
        host_name = request.POST.get("host_name", "").strip()
        has_searched = True

        if not host_name:
            error = "Enter a host name to search."
            status = 422
        else:
            try:
                results = locate_tenants(host_name)
            except Exception:
                logger.exception("Tenant lookup failed for host_name=%s", host_name)
                error = "Tenant lookup failed. Check the server logs for details."
                status = 500

    context = {
        "host_name": host_name,
        "results": results,
        "error": error,
        "has_searched": has_searched,
    }
    if request.headers.get("HX-Request"):
        return render(request, "partials/_locate_tenant.html", context, status=status)
    return render(request, "locate_tenant.html", context, status=status)


@login_required
@require_http_methods(["GET", "POST"])
def squelch(request: HttpRequest) -> HttpResponse:
    available_regions = get_squelch_regions()
    host_name = ""
    query = ""
    selected_regions = available_regions
    result: SquelchQueryResult | None = None
    error = ""
    has_searched = False
    status = 200

    if request.method == "POST":
        host_name = request.POST.get("host_name", "").strip()
        query = request.POST.get("query", "").strip()
        selected_regions = [
            region
            for region in request.POST.getlist("regions")
            if region in available_regions
        ]
        has_searched = True

        if not query:
            error = "Enter a query to search."
            status = 422
        elif not selected_regions:
            error = "Choose at least one region."
            status = 422
        else:
            try:
                result = run_squelch_query(host_name, query, selected_regions)
            except ModuleNotFoundError:
                logger.exception("Squelch tool is not available")
                error = "Squelch is not wired yet. Add tools/squelch.py with run_squelch(query, regions)."
                status = 500
            except Exception as exc:
                logger.exception(
                    "Squelch query failed for regions=%s", selected_regions
                )
                error = f"Squelch query failed: {exc}"
                status = 500

    context = {
        "available_regions": available_regions,
        "selected_regions": selected_regions,
        "host_name": host_name,
        "query": query,
        "result": result,
        "error": error,
        "has_searched": has_searched,
    }
    if request.headers.get("HX-Request"):
        return render(request, "partials/_squelch.html", context)
    return render(request, "squelch.html", context, status=status)


@login_required
@require_POST
def squelch_export(request: HttpRequest) -> HttpResponse:
    available_regions = get_squelch_regions()
    host_name = request.POST.get("host_name", "").strip()
    query = request.POST.get("query", "").strip()
    selected_regions = [
        region
        for region in request.POST.getlist("regions")
        if region in available_regions
    ]

    if not query:
        return _squelch_export_error(
            request,
            available_regions,
            selected_regions,
            host_name,
            query,
            "Enter a query to search.",
            422,
        )
    if not selected_regions:
        return _squelch_export_error(
            request,
            available_regions,
            selected_regions,
            host_name,
            query,
            "Choose at least one region.",
            422,
        )

    try:
        result = run_squelch_query(host_name, query, selected_regions)
    except Exception as exc:
        logger.exception("Squelch export failed for regions=%s", selected_regions)
        return _squelch_export_error(
            request,
            available_regions,
            selected_regions,
            host_name,
            query,
            f"Squelch export failed: {exc}",
            500,
        )

    response = HttpResponse(
        build_squelch_xlsx(result),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="squelch-results.xlsx"'
    return response


def _squelch_export_error(
    request: HttpRequest,
    available_regions: list[str],
    selected_regions: list[str],
    host_name: str,
    query: str,
    error: str,
    status: int,
) -> HttpResponse:
    return render(
        request,
        "squelch.html",
        {
            "available_regions": available_regions,
            "selected_regions": selected_regions,
            "host_name": host_name,
            "query": query,
            "result": None,
            "error": error,
            "has_searched": True,
        },
        status=status,
    )


@login_required
def commands(request: HttpRequest) -> HttpResponse:
    context = {
        "commands": COMMANDS.values(),
        "runs": CommandRun.objects.all()[:20],
        "schedules": CommandScheduleService.all(),
    }
    if request.headers.get("HX-Request"):
        return render(request, "partials/_commands.html", context)
    return render(request, "commands.html", context)


def _command_defaults() -> dict[str, dict[str, object]]:
    return {command.key: dict(command.default_params) for command in COMMANDS.values()}


@login_required
@require_http_methods(["GET", "POST"])
def command_run_create(request: HttpRequest) -> HttpResponse:
    initial_command_key = request.GET.get("command") or next(iter(COMMANDS))
    if request.method == "GET":
        response = render(
            request,
            "partials/_command_run_form.html",
            {
                "form": CommandRunForm(initial={"command_key": initial_command_key}),
                "form_action": reverse("command_run_create"),
                "modal_title": "Run Command",
                "command_defaults": _command_defaults(),
            },
        )
        response["HX-Trigger"] = "openCommandModal"
        return response

    form = CommandRunForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "partials/_command_run_form.html",
            {
                "form": form,
                "form_action": reverse("command_run_create"),
                "modal_title": "Run Command",
                "command_defaults": _command_defaults(),
            },
            status=422,
        )

    enqueue_command_run(form.cleaned_data["command_key"], form.params)
    response = render(
        request,
        "partials/_command_run_list.html",
        {"runs": CommandRun.objects.all()[:20]},
    )
    response["HX-Retarget"] = "#command-run-list"
    response["HX-Reswap"] = "innerHTML"
    response["HX-Trigger"] = "closeCommandModal"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def command_schedule_create(request: HttpRequest) -> HttpResponse:
    initial_command_key = request.GET.get("command") or next(iter(COMMANDS))
    if request.method == "GET":
        response = render(
            request,
            "partials/_command_schedule_form.html",
            {
                "form": CommandScheduleForm(
                    initial={"command_key": initial_command_key}
                ),
                "form_action": reverse("command_schedule_create"),
                "modal_title": "Schedule Command",
                "command_defaults": _command_defaults(),
            },
        )
        response["HX-Trigger"] = "openCommandModal"
        return response

    form = CommandScheduleForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "partials/_command_schedule_form.html",
            {
                "form": form,
                "form_action": reverse("command_schedule_create"),
                "modal_title": "Schedule Command",
                "command_defaults": _command_defaults(),
            },
            status=422,
        )

    CommandScheduleService.create(form.save())
    response = render(
        request,
        "partials/_command_schedule_list.html",
        {"schedules": CommandScheduleService.all()},
    )
    response["HX-Retarget"] = "#command-schedule-list"
    response["HX-Reswap"] = "innerHTML"
    response["HX-Trigger"] = "closeCommandModal"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def command_schedule_update(request: HttpRequest, pk: int) -> HttpResponse:
    schedule = get_object_or_404(CommandSchedule, pk=pk)
    if request.method == "GET":
        response = render(
            request,
            "partials/_command_schedule_form.html",
            {
                "form": CommandScheduleForm(instance=schedule),
                "form_action": reverse("command_schedule_update", args=[schedule.pk]),
                "modal_title": "Edit Schedule",
                "command_defaults": _command_defaults(),
            },
        )
        response["HX-Trigger"] = "openCommandModal"
        return response

    form = CommandScheduleForm(request.POST, instance=schedule)
    if not form.is_valid():
        return render(
            request,
            "partials/_command_schedule_form.html",
            {
                "form": form,
                "form_action": reverse("command_schedule_update", args=[schedule.pk]),
                "modal_title": "Edit Schedule",
                "command_defaults": _command_defaults(),
            },
            status=422,
        )

    CommandScheduleService.update(form.save())
    response = render(
        request,
        "partials/_command_schedule_list.html",
        {"schedules": CommandScheduleService.all()},
    )
    response["HX-Retarget"] = "#command-schedule-list"
    response["HX-Reswap"] = "innerHTML"
    response["HX-Trigger"] = "closeCommandModal"
    return response


@login_required
@require_POST
def command_schedule_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    CommandScheduleService.toggle(get_object_or_404(CommandSchedule, pk=pk))
    return render(
        request,
        "partials/_command_schedule_list.html",
        {"schedules": CommandScheduleService.all()},
    )


@login_required
@require_POST
def command_schedule_delete(request: HttpRequest, pk: int) -> HttpResponse:
    CommandScheduleService.delete(get_object_or_404(CommandSchedule, pk=pk))
    return render(
        request,
        "partials/_command_schedule_list.html",
        {"schedules": CommandScheduleService.all()},
    )


@login_required
@require_GET
def command_run_detail(request: HttpRequest, pk: int) -> HttpResponse:
    run = get_object_or_404(CommandRun, pk=pk)
    if run.command_key == "lock_monitor":
        context = {
            "detail_run": run,
            **_lock_monitor_context(request, command_run_id=run.pk),
        }
        if request.headers.get("HX-Request"):
            return render(request, "partials/_lock_monitor.html", context)
        return render(request, "lock_monitor.html", context)

    if request.headers.get("HX-Request"):
        return render(request, "partials/_command_run_detail.html", {"run": run})
    return render(request, "command_run_detail.html", {"run": run})
