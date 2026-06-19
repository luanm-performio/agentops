from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import text

from .db_config import Region, load_config
from .tenant import DataSource, Tunnel, find_tenant_data_sources

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def run_squelch(
    host_name: str,
    query: str,
    regions: list[str],
) -> list[dict[str, object]]:
    tenant_host_name = host_name.strip()
    sql = query.strip()
    if not sql:
        raise ValueError("Query is required.")
    if not _is_read_only_query(sql):
        raise ValueError("Only read-only SELECT queries are allowed.")

    configured_regions = load_config(str(_CONFIG_PATH))
    selected_regions = _select_regions(configured_regions, regions)
    if not selected_regions:
        raise ValueError("Choose at least one region.")

    data_sources = find_tenant_data_sources(tenant_host_name, selected_regions)
    if not data_sources:
        return []

    rows: list[dict[str, object]] = []
    for data_source in data_sources:
        if data_source.region is None:
            raise ValueError(
                f"Tenant data source has no region: {data_source.schema_name}"
            )
        with Tunnel(data_source, data_source.region) as engine:
            with engine.connect() as connection:
                result = connection.execute(text(sql)).mappings()
                rows.extend(_with_tenant(row, data_source) for row in result)

    return rows


def _is_read_only_query(sql: str) -> bool:
    return sql.lstrip(" \n\r\t(").lower().startswith("select")


def _select_regions(configured_regions: list[Region], names: list[str]) -> list[Region]:
    configured_by_name = {
        region.name: region for region in configured_regions if region.name
    }
    unknown_regions = [name for name in names if name not in configured_by_name]
    if unknown_regions:
        raise ValueError(f"Unknown region: {', '.join(unknown_regions)}")
    return [configured_by_name[name] for name in names]


def _with_tenant(
    row: Mapping[str, object], data_source: DataSource
) -> dict[str, object]:
    row_data = dict(row)
    region = data_source.region
    metadata = {
        _available_column_name(row_data, "region"): (
            region.name or region.remote_bind_address if region else ""
        ),
        _available_column_name(row_data, "database_server_url"): (
            data_source.database_server_url
        ),
        _available_column_name(row_data, "schema_name"): data_source.schema_name,
        _available_column_name(row_data, "shard_hosts"): data_source.shard_hosts or "",
    }
    return {**metadata, **row_data}


def _available_column_name(row: Mapping[str, object], preferred_name: str) -> str:
    if preferred_name not in row:
        return preferred_name
    return f"tenant_{preferred_name}"
