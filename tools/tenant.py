from dataclasses import dataclass, fields
from urllib.parse import urlsplit

from sqlalchemy import Engine, MetaData, Table, create_engine, select
from sqlalchemy.pool import NullPool
from typing_extensions import Any

from .db_config import Region
from .tunnel import SSHTunnel, switch_vnet


def map_to_dataclass(cls, row):
    field_names = {f.name for f in fields(cls)}
    filtered = {
        field_name: value
        for field_name, value in row.items()
        if field_name in field_names
    }

    return cls(**filtered)


@dataclass(frozen=True)
class DataSource:
    username: str
    password: str
    database_server_url: str
    schema_name: str
    region: Region | None = None
    shard_hosts: str | None = None
    name: str | None = None


def construct_uri(data_source: DataSource) -> str:
    return f"mysql+pymysql://{data_source.username}:{data_source.password}@{data_source.database_server_url}/{data_source.schema_name}"


def _database_server_address(
    database_server_url: str,
    default_port: int,
) -> tuple[str, int]:
    parsed = urlsplit(f"//{database_server_url.strip()}")
    if parsed.hostname is None:
        raise ValueError(f"Invalid database server URL: {database_server_url}")
    return parsed.hostname, parsed.port or default_port


class Tunnel:
    """Context manager that returns a SQLAlchemy Engine, routing via SSH, VPN, or direct."""

    def __init__(self, data_source: DataSource, region: Region) -> None:
        self.data_source = data_source
        self.region = region
        self._ssh_tunnel: SSHTunnel | None = None

    def __enter__(self) -> Engine:
        remote_host, remote_port = _database_server_address(
            self.data_source.database_server_url,
            self.region.remote_bind_port,
        )
        if self.region.devbox_required and self.region.devbox:
            self._ssh_tunnel = SSHTunnel(
                dev_box=self.region.devbox,
                remote_host=remote_host,
                remote_port=remote_port,
            )
            local = self._ssh_tunnel.start()
            ds = DataSource(
                database_server_url=f"{local.host}:{local.port}",
                schema_name=self.data_source.schema_name,
                username=self.data_source.username,
                password=self.data_source.password,
            )
            self.engine = create_engine(
                construct_uri(ds),
                poolclass=NullPool,
                connect_args={
                    "ssl": {
                        "ca": self.region.devbox.ca_file,
                        "check_hostname": False,
                        "verify_server_cert": False,
                    }
                },
            )
        else:
            if self.region.virtual_network_id:
                switch_vnet(
                    self.region.virtual_network_id,
                    probe_host=remote_host,
                    probe_port=remote_port,
                )
            self.engine = create_engine(
                construct_uri(self.data_source), poolclass=NullPool
            )

        return self.engine

    def __exit__(self, exc_type: Any, exc_value: Any, exc_traceback: Any) -> None:
        self.engine.dispose()
        if self._ssh_tunnel:
            self._ssh_tunnel.stop()


def find_tenant_data_sources(host_name: str, regions: list[Region]) -> list[DataSource]:
    search = host_name.strip()
    data_sources = []
    for region in regions:
        global_data_source = DataSource(
            username=region.username,
            password=region.password,
            database_server_url=region.remote_bind_address,
            schema_name="performancecentre_global",
        )
        try:
            with Tunnel(global_data_source, region) as engine:
                with engine.connect() as connection:
                    metadata = MetaData()
                    data_source = Table("data_source", metadata, autoload_with=engine)
                    query = select(data_source)
                    if search:
                        query = query.where(
                            data_source.c.shard_hosts.like(f"%{search}%")
                        )
                    rows = connection.execute(query).mappings()

                    region_data_sources = [
                        DataSource(
                            shard_hosts=row["shard_hosts"],
                            username=region.username,
                            password=region.password,
                            database_server_url=row["database_server_url"],
                            schema_name=row["schema_name"],
                            region=region,
                            name=region.name,
                        )
                        for row in rows
                    ]

                    if region_data_sources:
                        data_sources += region_data_sources
        except Exception as e:
            print(f"Skipping {region.name or region.remote_bind_address}: {e}")

    return data_sources


class Tenant:
    def find_by_host_name(
        self, host_name: str, regions: list[Region]
    ) -> list[DataSource]:
        return find_tenant_data_sources(host_name, regions)
