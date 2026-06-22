import json
from collections.abc import Iterator, Mapping
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from django.utils import timezone
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from tools.db_config import load_config
from tools.tenant import DataSource, Tunnel, find_tenant_data_sources

from .models import LockMonitorCapture, LockMonitoringRecord

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "tools" / "config.yaml"

_SYS_LOCK_WAITS_QUERY = """
SELECT
    waiting_pid,
    waiting_query,
    waiting_age_secs,
    locked_type AS waiting_lock_type,
    waiting_lock_mode,
    blocking_pid,
    blocking_query,
    locked_type AS blocking_lock_type,
    blocking_lock_mode,
    locked_table_schema,
    locked_table_name,
    locked_table,
    locked_index
FROM sys.innodb_lock_waits
"""

_PERFORMANCE_SCHEMA_LOCK_WAITS_QUERY = """
SELECT
    waiting_thread.PROCESSLIST_ID AS waiting_pid,
    waiting_statement.SQL_TEXT AS waiting_query,
    waiting_lock.LOCK_TYPE AS waiting_lock_type,
    waiting_lock.LOCK_MODE AS waiting_lock_mode,
    blocking_thread.PROCESSLIST_ID AS blocking_pid,
    blocking_statement.SQL_TEXT AS blocking_query,
    blocking_lock.LOCK_TYPE AS blocking_lock_type,
    blocking_lock.LOCK_MODE AS blocking_lock_mode,
    waiting_lock.OBJECT_SCHEMA AS locked_table_schema,
    waiting_lock.OBJECT_NAME AS locked_table_name,
    CONCAT_WS('.', waiting_lock.OBJECT_SCHEMA, waiting_lock.OBJECT_NAME) AS locked_table,
    waiting_lock.INDEX_NAME AS locked_index
FROM performance_schema.data_lock_waits AS waits
JOIN performance_schema.data_locks AS waiting_lock
  ON waiting_lock.ENGINE_LOCK_ID = waits.REQUESTING_ENGINE_LOCK_ID
JOIN performance_schema.data_locks AS blocking_lock
  ON blocking_lock.ENGINE_LOCK_ID = waits.BLOCKING_ENGINE_LOCK_ID
LEFT JOIN performance_schema.threads AS waiting_thread
  ON waiting_thread.THREAD_ID = waits.REQUESTING_THREAD_ID
LEFT JOIN performance_schema.threads AS blocking_thread
  ON blocking_thread.THREAD_ID = waits.BLOCKING_THREAD_ID
LEFT JOIN performance_schema.events_statements_current AS waiting_statement
  ON waiting_statement.THREAD_ID = waits.REQUESTING_THREAD_ID
LEFT JOIN performance_schema.events_statements_current AS blocking_statement
  ON blocking_statement.THREAD_ID = waits.BLOCKING_THREAD_ID
"""

_INFORMATION_SCHEMA_LOCK_WAITS_QUERY = """
SELECT
    waiting.trx_mysql_thread_id AS waiting_pid,
    waiting.trx_query AS waiting_query,
    TIMESTAMPDIFF(SECOND, waiting.trx_wait_started, NOW()) AS waiting_age_secs,
    blocking.trx_mysql_thread_id AS blocking_pid,
    blocking.trx_query AS blocking_query,
    waiting.trx_state AS waiting_lock_mode,
    blocking.trx_state AS blocking_lock_mode,
    waiting.trx_requested_lock_id AS waiting_lock_id,
    blocking.trx_tables_locked AS blocking_tables_locked
FROM information_schema.innodb_lock_waits AS waits
JOIN information_schema.innodb_trx AS waiting
  ON waiting.trx_id = waits.requesting_trx_id
JOIN information_schema.innodb_trx AS blocking
  ON blocking.trx_id = waits.blocking_trx_id
"""

_SLEEPING_LOCK_BLOCKERS_QUERY = """
SELECT DISTINCT
    waiting_thread.PROCESSLIST_ID AS waiting_pid,
    waiting_thread.PROCESSLIST_DB AS waiting_schema,
    waiting_thread.PROCESSLIST_TIME AS waiting_age_secs,
    waiting_thread.PROCESSLIST_INFO AS waiting_query,
    blocking_thread.PROCESSLIST_ID AS blocking_pid,
    blocking_thread.PROCESSLIST_COMMAND AS blocking_command,
    blocking_thread.PROCESSLIST_TIME AS blocking_sleep_seconds,
    blocking_thread.PROCESSLIST_DB AS blocking_schema,
    blocking_transaction.TRX_STARTED AS blocking_transaction_started,
    TIMESTAMPDIFF(
        SECOND,
        blocking_transaction.TRX_STARTED,
        NOW()
    ) AS blocking_transaction_seconds,
    blocking_transaction.TRX_ROWS_LOCKED AS blocking_rows_locked,
    blocking_transaction.TRX_ROWS_MODIFIED AS blocking_rows_modified,
    blocking_lock.OBJECT_SCHEMA AS locked_table_schema,
    blocking_lock.OBJECT_NAME AS locked_table_name,
    blocking_lock.INDEX_NAME AS locked_index,
    blocking_lock.LOCK_TYPE AS blocking_lock_type,
    blocking_lock.LOCK_MODE AS blocking_lock_mode,
    COALESCE(
        current_statement.SQL_TEXT,
        history_statement.SQL_TEXT
    ) AS blocking_last_query
FROM performance_schema.data_lock_waits AS waits
JOIN performance_schema.threads AS waiting_thread
  ON waiting_thread.THREAD_ID = waits.REQUESTING_THREAD_ID
JOIN performance_schema.threads AS blocking_thread
  ON blocking_thread.THREAD_ID = waits.BLOCKING_THREAD_ID
JOIN performance_schema.data_locks AS blocking_lock
  ON blocking_lock.ENGINE = waits.ENGINE
 AND blocking_lock.ENGINE_LOCK_ID = waits.BLOCKING_ENGINE_LOCK_ID
LEFT JOIN information_schema.INNODB_TRX AS blocking_transaction
  ON blocking_transaction.TRX_MYSQL_THREAD_ID = blocking_thread.PROCESSLIST_ID
LEFT JOIN performance_schema.events_statements_current AS current_statement
  ON current_statement.THREAD_ID = waits.BLOCKING_THREAD_ID
 AND current_statement.EVENT_ID = waits.BLOCKING_EVENT_ID
LEFT JOIN performance_schema.events_statements_history AS history_statement
  ON history_statement.THREAD_ID = waits.BLOCKING_THREAD_ID
 AND history_statement.EVENT_ID = waits.BLOCKING_EVENT_ID
WHERE LOWER(blocking_thread.PROCESSLIST_COMMAND) = 'sleep'
"""

_STATEMENT_METRICS_QUERY = """
SELECT
    threads.PROCESSLIST_ID AS process_id,
    statements.TIMER_WAIT AS timer_wait,
    statements.LOCK_TIME AS lock_time,
    statements.ROWS_AFFECTED AS rows_affected,
    statements.ROWS_SENT AS rows_sent,
    statements.ROWS_EXAMINED AS rows_examined,
    statements.CREATED_TMP_TABLES AS created_tmp_tables,
    statements.CREATED_TMP_DISK_TABLES AS created_tmp_disk_tables,
    statements.SELECT_FULL_JOIN AS select_full_join,
    statements.SELECT_SCAN AS select_scan,
    statements.SORT_MERGE_PASSES AS sort_merge_passes,
    statements.SORT_ROWS AS sort_rows,
    statements.NO_INDEX_USED AS no_index_used,
    statements.NO_GOOD_INDEX_USED AS no_good_index_used
FROM performance_schema.events_statements_current AS statements
JOIN performance_schema.threads AS threads
  ON threads.THREAD_ID = statements.THREAD_ID
WHERE threads.PROCESSLIST_ID IS NOT NULL
"""

_GLOBAL_STATUS_QUERY = """
SHOW GLOBAL STATUS WHERE Variable_name IN (
    'Threads_connected',
    'Threads_running',
    'Slow_queries',
    'Created_tmp_tables',
    'Created_tmp_disk_tables',
    'Innodb_buffer_pool_reads',
    'Innodb_buffer_pool_read_requests',
    'Innodb_buffer_pool_wait_free',
    'Innodb_row_lock_current_waits',
    'Innodb_row_lock_time',
    'Innodb_row_lock_waits',
    'Uptime'
)
"""

_GLOBAL_VARIABLES_QUERY = """
SHOW GLOBAL VARIABLES WHERE Variable_name IN (
    'max_connections',
    'innodb_buffer_pool_size'
)
"""


def run_lock_monitor(
    tenant_host: str,
    *,
    long_running_seconds: int = 300,
    command_run_id: int | None = None,
) -> LockMonitorCapture:
    host = tenant_host.strip()
    if not host:
        raise ValueError("tenant_host is required.")

    capture = LockMonitorCapture.objects.create(
        command_run_id=command_run_id,
        tenant_host=host,
    )
    warnings: list[str] = []
    connected_data_sources = 0
    records: list[LockMonitoringRecord] = []

    try:
        regions = load_config(str(_CONFIG_PATH))
        data_sources = find_tenant_data_sources(host, regions)
        capture.data_source_count = len(data_sources)
        if not data_sources:
            raise ValueError(f"No tenant data sources found for host: {host}")

        for data_source in data_sources:
            if data_source.region is None:
                warnings.append(
                    f"{data_source.schema_name}: tenant data source has no region"
                )
                continue

            try:
                data_source_records, data_source_warnings = _inspect_data_source(
                    capture,
                    data_source,
                    long_running_seconds,
                )
            except Exception as exc:
                warnings.append(f"{data_source.schema_name}: {exc}")
                continue

            connected_data_sources += 1
            records.extend(data_source_records)
            warnings.extend(data_source_warnings)

        if connected_data_sources == 0:
            details = "; ".join(warnings) or "No database connection succeeded."
            raise RuntimeError(details)

        LockMonitoringRecord.objects.bulk_create(records)
        capture.process_count = sum(
            record.record_type == LockMonitoringRecord.PROCESS for record in records
        )
        capture.lock_count = sum(
            record.record_type == LockMonitoringRecord.LOCK_WAIT for record in records
        )
        capture.warning = "\n".join(warnings)
        capture.status = LockMonitorCapture.COMPLETED
        capture.completed_at = timezone.now()
        capture.save(
            update_fields=[
                "data_source_count",
                "process_count",
                "lock_count",
                "warning",
                "status",
                "completed_at",
            ]
        )
        return capture
    except Exception as exc:
        capture.status = LockMonitorCapture.FAILED
        capture.error = str(exc)
        capture.warning = "\n".join(warnings)
        capture.completed_at = timezone.now()
        capture.save(
            update_fields=[
                "data_source_count",
                "warning",
                "error",
                "status",
                "completed_at",
            ]
        )
        raise


def _inspect_data_source(
    capture: LockMonitorCapture,
    data_source: DataSource,
    long_running_seconds: int,
) -> tuple[list[LockMonitoringRecord], list[str]]:
    region = data_source.region
    if region is None:
        raise ValueError("Tenant data source has no region.")

    with Tunnel(data_source, region) as engine:
        with engine.connect() as connection:
            all_process_rows = _fetch_mapping_rows(connection, "SHOW FULL PROCESSLIST")
            lock_rows, lock_warning = _fetch_lock_rows(connection)
            sleeping_blockers, sleeping_blocker_warning = _fetch_sleeping_lock_blockers(
                connection
            )
            lock_rows = _merge_sleeping_blocker_details(
                lock_rows,
                sleeping_blockers,
            )
            lock_process_ids = {
                process_id
                for row in lock_rows
                for process_id in (
                    _integer_value(row, "waiting_pid"),
                    _integer_value(row, "blocking_pid"),
                )
                if process_id is not None
            }
            process_rows = [
                row
                for row in all_process_rows
                if _is_running_process(row)
                or _integer_value(row, "Id") in lock_process_ids
            ]
            records = [
                _process_record(capture, data_source, row) for row in process_rows
            ]
            statement_metrics, statement_warning = _fetch_statement_metrics(connection)
            server_metrics, server_warning = _fetch_server_metrics(connection)
            _enrich_process_records(
                records,
                lock_rows=lock_rows,
                statement_metrics=statement_metrics,
                server_metrics=server_metrics,
                tenant_schema=data_source.schema_name,
                long_running_seconds=long_running_seconds,
            )
            for record in records:
                _add_explain_if_long_running(
                    connection,
                    record,
                    long_running_seconds=long_running_seconds,
                    tenant_schema=data_source.schema_name,
                )

    records.extend(
        _lock_record(
            capture,
            data_source,
            row,
            server_metrics=server_metrics,
        )
        for row in lock_rows
    )
    records.append(
        _resource_record(
            capture,
            data_source,
            server_metrics=server_metrics,
        )
    )
    warnings = [
        f"{data_source.schema_name}: {warning}"
        for warning in (
            lock_warning,
            sleeping_blocker_warning,
            statement_warning,
            server_warning,
        )
        if warning
    ]
    return records, warnings


def _fetch_lock_rows(
    connection: Connection,
) -> tuple[list[dict[str, object]], str]:
    errors: list[str] = []
    for label, query in [
        ("sys.innodb_lock_waits", _SYS_LOCK_WAITS_QUERY),
        (
            "performance_schema.data_lock_waits",
            _PERFORMANCE_SCHEMA_LOCK_WAITS_QUERY,
        ),
        (
            "information_schema.innodb_lock_waits",
            _INFORMATION_SCHEMA_LOCK_WAITS_QUERY,
        ),
    ]:
        try:
            return _fetch_mapping_rows(connection, query), ""
        except SQLAlchemyError as exc:
            errors.append(f"{label} unavailable: {exc}")
    return [], "; ".join(errors)


def _fetch_sleeping_lock_blockers(
    connection: Connection,
) -> tuple[list[dict[str, object]], str]:
    try:
        return _fetch_mapping_rows(connection, _SLEEPING_LOCK_BLOCKERS_QUERY), ""
    except SQLAlchemyError as exc:
        return [], f"sleeping lock blocker details unavailable: {exc}"


def _merge_sleeping_blocker_details(
    lock_rows: list[dict[str, object]],
    sleeping_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged_rows = [dict(row) for row in lock_rows]
    rows_by_process_pair = {
        (
            _integer_value(row, "waiting_pid"),
            _integer_value(row, "blocking_pid"),
        ): row
        for row in merged_rows
    }

    for sleeping_row in sleeping_rows:
        key = (
            _integer_value(sleeping_row, "waiting_pid"),
            _integer_value(sleeping_row, "blocking_pid"),
        )
        target = rows_by_process_pair.get(key)
        if target is None:
            target = dict(sleeping_row)
            merged_rows.append(target)
            rows_by_process_pair[key] = target
        else:
            target.update(
                {
                    str(field): value
                    for field, value in sleeping_row.items()
                    if value not in (None, "")
                }
            )

        if not _string_value(target, "blocking_query"):
            target["blocking_query"] = _mapping_value(
                sleeping_row,
                "blocking_last_query",
            )
    return merged_rows


def _fetch_mapping_rows(
    connection: Connection,
    query: str,
) -> list[dict[str, object]]:
    result = connection.execute(text(query)).mappings()
    return [{str(key): value for key, value in row.items()} for row in result]


def _fetch_statement_metrics(
    connection: Connection,
) -> tuple[dict[int, dict[str, object]], str]:
    try:
        rows = _fetch_mapping_rows(connection, _STATEMENT_METRICS_QUERY)
    except SQLAlchemyError as exc:
        return {}, f"statement metrics unavailable: {exc}"

    metrics_by_process: dict[int, dict[str, object]] = {}
    for row in rows:
        process_id = _integer_value(row, "process_id")
        if process_id is None:
            continue
        metrics = {
            str(key).lower(): _metric_value(value)
            for key, value in row.items()
            if str(key).lower() != "process_id"
        }
        metrics["timer_wait_ms"] = _picoseconds_to_milliseconds(
            metrics.pop("timer_wait", None)
        )
        metrics["lock_time_ms"] = _picoseconds_to_milliseconds(
            metrics.pop("lock_time", None)
        )
        metrics_by_process[process_id] = metrics
    return metrics_by_process, ""


def _fetch_server_metrics(
    connection: Connection,
) -> tuple[dict[str, object], str]:
    warnings: list[str] = []
    metrics: dict[str, object] = {}
    for label, query in (
        ("global status", _GLOBAL_STATUS_QUERY),
        ("global variables", _GLOBAL_VARIABLES_QUERY),
    ):
        try:
            rows = _fetch_mapping_rows(connection, query)
        except SQLAlchemyError as exc:
            warnings.append(f"{label} unavailable: {exc}")
            continue
        for row in rows:
            name = _string_value(row, "Variable_name").strip().lower()
            if name:
                metrics[name] = _metric_value(_mapping_value(row, "Value"))

    threads_connected = _metric_number(metrics, "threads_connected")
    max_connections = _metric_number(metrics, "max_connections")
    if threads_connected is not None and max_connections:
        metrics["connection_utilization_pct"] = round(
            threads_connected / max_connections * 100,
            2,
        )

    temp_tables = _metric_number(metrics, "created_tmp_tables")
    disk_temp_tables = _metric_number(metrics, "created_tmp_disk_tables")
    if temp_tables and disk_temp_tables is not None:
        metrics["temp_disk_table_pct"] = round(
            disk_temp_tables / temp_tables * 100,
            2,
        )

    buffer_requests = _metric_number(metrics, "innodb_buffer_pool_read_requests")
    buffer_reads = _metric_number(metrics, "innodb_buffer_pool_reads")
    if buffer_requests and buffer_reads is not None:
        metrics["buffer_pool_disk_read_pct"] = round(
            buffer_reads / buffer_requests * 100,
            4,
        )
    return metrics, "; ".join(warnings)


def _enrich_process_records(
    records: list[LockMonitoringRecord],
    *,
    lock_rows: list[dict[str, object]],
    statement_metrics: dict[int, dict[str, object]],
    server_metrics: dict[str, object],
    tenant_schema: str,
    long_running_seconds: int,
) -> None:
    records_by_process = {
        record.process_id: record for record in records if record.process_id is not None
    }
    for record in records:
        record.is_tenant_schema = _schemas_match(
            record.database_name,
            tenant_schema,
        )
        record.is_long_running = (record.duration_seconds or 0) >= long_running_seconds
        record.statement_metrics = statement_metrics.get(record.process_id or -1, {})
        record.server_metrics = dict(server_metrics)

    for lock_row in lock_rows:
        waiting_id = _integer_value(lock_row, "waiting_pid")
        blocking_id = _integer_value(lock_row, "blocking_pid")
        waiting_record = records_by_process.get(waiting_id)
        if waiting_record is not None:
            waiting_record.is_lock_waiting = True
            waiting_record.blocking_process_id = (
                waiting_record.blocking_process_id or blocking_id
            )
        blocking_record = records_by_process.get(blocking_id)
        if blocking_record is not None:
            blocking_record.is_lock_blocking = True
            if _string_value(lock_row, "blocking_command").casefold() == "sleep":
                blocking_record.statement_metrics = {
                    **blocking_record.statement_metrics,
                    "blocking_command": "Sleep",
                    "sleeping_seconds": _integer_value(
                        lock_row,
                        "blocking_sleep_seconds",
                    ),
                    "transaction_started": _json_value(
                        _mapping_value(lock_row, "blocking_transaction_started")
                    ),
                    "transaction_seconds": _integer_value(
                        lock_row,
                        "blocking_transaction_seconds",
                    ),
                    "rows_locked": _integer_value(
                        lock_row,
                        "blocking_rows_locked",
                    ),
                    "rows_modified": _integer_value(
                        lock_row,
                        "blocking_rows_modified",
                    ),
                    "last_query": _string_value(
                        lock_row,
                        "blocking_last_query",
                    ),
                }

    for record in records:
        record.contention_signals = _contention_signals(record)


def _contention_signals(record: LockMonitoringRecord) -> list[str]:
    signals: list[str] = []
    statement = record.statement_metrics
    server = record.server_metrics

    if record.is_lock_waiting:
        signals.append("waiting_on_lock")
    if record.is_lock_blocking:
        signals.append("blocking_other_query")
    if (
        record.is_lock_blocking
        and str(statement.get("blocking_command", "")).casefold() == "sleep"
    ):
        signals.append("sleeping_transaction_holds_lock")
    if (_metric_number(statement, "lock_time_ms") or 0) > 0:
        signals.append("statement_lock_time")
    if (_metric_number(statement, "created_tmp_disk_tables") or 0) > 0:
        signals.append("statement_temp_disk_tables")
    if (_metric_number(statement, "no_index_used") or 0) > 0:
        signals.append("statement_no_index_used")
    if (_metric_number(statement, "no_good_index_used") or 0) > 0:
        signals.append("statement_no_good_index")
    if (_metric_number(statement, "sort_merge_passes") or 0) > 0:
        signals.append("statement_sort_merge_passes")

    rows_examined = _metric_number(statement, "rows_examined") or 0
    rows_sent = _metric_number(statement, "rows_sent") or 0
    if rows_examined >= 10_000 and rows_examined / max(rows_sent, 1) >= 100:
        signals.append("high_rows_examined_to_sent")
    if (_metric_number(server, "connection_utilization_pct") or 0) >= 80:
        signals.append("server_connection_utilization_high")
    if (_metric_number(server, "innodb_row_lock_current_waits") or 0) > 0:
        signals.append("server_innodb_lock_waits_active")
    return signals


def _server_contention_signals(server_metrics: Mapping[str, object]) -> list[str]:
    record = LockMonitoringRecord(server_metrics=dict(server_metrics))
    return _contention_signals(record)


def _metric_value(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    text_value = str(value).strip()
    try:
        return int(text_value)
    except ValueError:
        try:
            return float(text_value)
        except ValueError:
            return text_value


def _metric_number(metrics: Mapping[str, object], key: str) -> float | None:
    value = metrics.get(key)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _picoseconds_to_milliseconds(value: object) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value) / 1_000_000_000, 3)
    except (TypeError, ValueError):
        return None


def _is_running_process(row: Mapping[str, object]) -> bool:
    command = _string_value(row, "Command").strip().lower()
    query_text = _string_value(row, "Info").strip().lower()
    return command != "sleep" and query_text != "show full processlist"


def _add_explain_if_long_running(
    connection: Connection,
    record: LockMonitoringRecord,
    *,
    long_running_seconds: int,
    tenant_schema: str,
) -> None:
    duration_seconds = record.duration_seconds or 0
    record.is_tenant_schema = _schemas_match(record.database_name, tenant_schema)
    record.is_long_running = duration_seconds >= long_running_seconds
    if not record.is_long_running:
        return
    if not record.is_tenant_schema:
        database_name = record.database_name or "no current database"
        record.explain_skipped_reason = (
            f"Not explained: process database {database_name!r} does not match "
            f"tenant schema {tenant_schema!r}."
        )
        return
    if not record.query_text.strip():
        record.explain_skipped_reason = "Not explained: query text is empty."
        return

    if not _is_explainable_query(record.query_text):
        record.explain_skipped_reason = (
            "Not explained: query type is not supported by EXPLAIN FORMAT=JSON."
        )
        return

    try:
        plan = _explain_query(connection, record.query_text)
        record.explain_json = plan
        record.explanation = summarize_mysql_explain(plan)
        record.explain_generated_at = timezone.now()
    except Exception as exc:
        record.explain_error = str(exc)


def _schemas_match(database_name: str, tenant_schema: str) -> bool:
    return database_name.strip(" `").casefold() == tenant_schema.strip(" `").casefold()


def _is_explainable_query(query: str) -> bool:
    sql = _strip_leading_sql_comments(query).strip().rstrip(";").strip()
    if not sql or ";" in sql:
        return False
    statement = sql.split(None, 1)[0].lower()
    return statement in {"select", "with", "delete", "insert", "replace", "update"}


def _strip_leading_sql_comments(query: str) -> str:
    sql = query
    while True:
        stripped = sql.lstrip()
        if stripped.startswith("/*"):
            comment_end = stripped.find("*/")
            if comment_end == -1:
                return stripped
            sql = stripped[comment_end + 2 :]
            continue
        if stripped.startswith("--") or stripped.startswith("#"):
            newline = stripped.find("\n")
            if newline == -1:
                return ""
            sql = stripped[newline + 1 :]
            continue
        return stripped


def _explain_query(connection: Connection, query: str) -> dict[str, object]:
    sql = _strip_leading_sql_comments(query).strip().rstrip(";").strip()
    result = connection.exec_driver_sql(f"EXPLAIN FORMAT=JSON {sql}")
    row = result.first()
    if row is None:
        raise RuntimeError("EXPLAIN FORMAT=JSON returned no result.")

    raw_plan = row[0]
    if isinstance(raw_plan, bytes):
        raw_plan = raw_plan.decode(errors="replace")
    if isinstance(raw_plan, str):
        parsed = json.loads(raw_plan)
    elif isinstance(raw_plan, Mapping):
        parsed = dict(raw_plan)
    else:
        raise TypeError(f"Unexpected EXPLAIN result type: {type(raw_plan).__name__}")
    if not isinstance(parsed, dict):
        raise TypeError("EXPLAIN FORMAT=JSON must return a JSON object.")
    return {str(key): _json_plan_value(value) for key, value in parsed.items()}


def summarize_mysql_explain(plan: Mapping[str, object]) -> str:
    lines: list[str] = []
    query_cost = _first_plan_value(plan, "query_cost", "estimated_total_cost")
    if query_cost is not None:
        lines.append(f"Estimated query cost: {query_cost}")

    table_number = 0
    for node in _walk_plan(plan):
        table_name = node.get("table_name")
        if not table_name:
            continue
        table_number += 1
        access_type = node.get("access_type") or node.get("index_access_type")
        chosen_key = node.get("key") or node.get("index_name")
        possible_keys = node.get("possible_keys")
        estimated_rows = (
            node.get("rows_examined_per_scan")
            or node.get("estimated_rows")
            or node.get("rows_produced_per_join")
        )
        filtered = node.get("filtered")
        operation = node.get("operation")

        details = [f"Table {table_name}"]
        if access_type:
            details.append(f"access={access_type}")
        details.append(f"key={chosen_key or 'none'}")
        if possible_keys:
            details.append(f"possible_keys={_display_plan_value(possible_keys)}")
        if estimated_rows is not None:
            details.append(f"estimated_rows={estimated_rows}")
        if filtered is not None:
            details.append(f"filtered={filtered}%")
        lines.append(f"{table_number}. " + "; ".join(details))
        if operation:
            lines.append(f"   Operation: {operation}")

        normalized_access = str(access_type or "").lower()
        normalized_operation = str(operation or "").lower()
        if normalized_access == "all" or "table scan" in normalized_operation:
            lines.append(f"   Warning: full table scan on {table_name}.")

    if table_number == 0:
        operation = _first_plan_value(plan, "operation")
        if operation:
            lines.append(f"Operation: {operation}")
        else:
            lines.append("Plan captured; no table access nodes were identified.")
    return "\n".join(lines)


def _walk_plan(value: object) -> Iterator[Mapping[str, object]]:
    if isinstance(value, Mapping):
        node = {str(key): child for key, child in value.items()}
        yield node
        for child in node.values():
            yield from _walk_plan(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_plan(child)


def _first_plan_value(plan: Mapping[str, object], *keys: str) -> object | None:
    for node in _walk_plan(plan):
        for key in keys:
            if key in node:
                return node[key]
    return None


def _display_plan_value(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _json_plan_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_plan_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_plan_value(child) for child in value]
    return _json_value(value)


def _process_record(
    capture: LockMonitorCapture,
    data_source: DataSource,
    row: Mapping[str, object],
) -> LockMonitoringRecord:
    return LockMonitoringRecord(
        capture=capture,
        record_type=LockMonitoringRecord.PROCESS,
        **_data_source_fields(data_source),
        process_id=_integer_value(row, "Id"),
        user=_string_value(row, "User"),
        host=_string_value(row, "Host"),
        database_name=_string_value(row, "db"),
        command=_string_value(row, "Command"),
        duration_seconds=_integer_value(row, "Time"),
        state=_string_value(row, "State"),
        query_text=_string_value(row, "Info"),
        raw_data=_json_mapping(row),
    )


def _lock_record(
    capture: LockMonitorCapture,
    data_source: DataSource,
    row: Mapping[str, object],
    *,
    server_metrics: dict[str, object],
) -> LockMonitoringRecord:
    state_parts = [
        _string_value(row, "locked_table"),
        _string_value(row, "locked_index"),
        _string_value(row, "waiting_lock_type"),
        _string_value(row, "waiting_lock_mode"),
    ]
    locked_schema = _string_value(row, "locked_table_schema")
    blocking_command = _string_value(row, "blocking_command")
    blocking_query = _string_value(row, "blocking_query") or _string_value(
        row,
        "blocking_last_query",
    )
    transaction_metrics = {
        "blocking_command": blocking_command,
        "sleeping_seconds": _integer_value(row, "blocking_sleep_seconds"),
        "transaction_started": _json_value(
            _mapping_value(row, "blocking_transaction_started")
        ),
        "transaction_seconds": _integer_value(
            row,
            "blocking_transaction_seconds",
        ),
        "rows_locked": _integer_value(row, "blocking_rows_locked"),
        "rows_modified": _integer_value(row, "blocking_rows_modified"),
    }
    record = LockMonitoringRecord(
        capture=capture,
        record_type=LockMonitoringRecord.LOCK_WAIT,
        **_data_source_fields(data_source),
        waiting_process_id=_integer_value(row, "waiting_pid"),
        blocking_process_id=_integer_value(row, "blocking_pid"),
        duration_seconds=_integer_value(row, "waiting_age_secs"),
        state=" | ".join(part for part in state_parts if part),
        query_text=_string_value(row, "waiting_query"),
        blocking_query_text=blocking_query,
        is_tenant_schema=_schemas_match(locked_schema, data_source.schema_name),
        is_lock_waiting=True,
        statement_metrics={
            key: value
            for key, value in transaction_metrics.items()
            if value is not None
        },
        server_metrics=dict(server_metrics),
        raw_data=_json_mapping(row),
    )
    record.contention_signals = [
        "waiting_on_lock",
        *(
            ["sleeping_transaction_blocker"]
            if blocking_command.casefold() == "sleep"
            else []
        ),
        *_server_contention_signals(server_metrics),
    ]
    return record


def _resource_record(
    capture: LockMonitorCapture,
    data_source: DataSource,
    *,
    server_metrics: dict[str, object],
) -> LockMonitoringRecord:
    return LockMonitoringRecord(
        capture=capture,
        record_type=LockMonitoringRecord.RESOURCE,
        **_data_source_fields(data_source),
        database_name=data_source.schema_name,
        is_tenant_schema=True,
        server_metrics=dict(server_metrics),
        contention_signals=_server_contention_signals(server_metrics),
    )


def _data_source_fields(data_source: DataSource) -> dict[str, str]:
    region = data_source.region
    return {
        "region": region.name or region.remote_bind_address if region else "",
        "database_server_url": data_source.database_server_url,
        "schema_name": data_source.schema_name,
    }


def _mapping_value(row: Mapping[str, object], key: str) -> object | None:
    lowered_key = key.lower()
    for row_key, value in row.items():
        if str(row_key).lower() == lowered_key:
            return value
    return None


def _string_value(row: Mapping[str, object], key: str) -> str:
    value = _mapping_value(row, key)
    return "" if value is None else str(value)


def _integer_value(row: Mapping[str, object], key: str) -> int | None:
    value = _mapping_value(row, key)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_mapping(row: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _json_value(value) for key, value in row.items()}


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)
