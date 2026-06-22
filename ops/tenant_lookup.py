from dataclasses import dataclass
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "tools" / "config.yaml"


@dataclass(frozen=True)
class TenantLookupResult:
    shard_hosts: str
    database_server_url: str
    schema_name: str
    region: str
    name: str

    @property
    def copy_text(self) -> str:
        return f"{self.database_server_url} {self.schema_name}"


def locate_tenants(host_name: str) -> list[TenantLookupResult]:
    search = host_name.strip()
    if not search:
        return []

    from tools.db_config import load_config  # noqa: PLC0415
    from tools.tenant import Tenant  # noqa: PLC0415

    regions = load_config(str(_CONFIG_PATH))
    data_sources = Tenant().find_by_host_name(search, regions)

    return [
        TenantLookupResult(
            shard_hosts=data_source.shard_hosts or "",
            database_server_url=data_source.database_server_url,
            schema_name=data_source.schema_name,
            region=data_source.region.remote_bind_address if data_source.region else "",
            name=data_source.name or "",
        )
        for data_source in data_sources
    ]
