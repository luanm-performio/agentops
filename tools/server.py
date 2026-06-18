import asyncio
import json
from pathlib import Path
from typing import Annotated

from claude_agent_sdk import create_sdk_mcp_server, tool

from .db_config import load_config
from .tenant import DataSource, Tenant

_config_path = Path(__file__).parent / "config.yaml"


def _ds_to_dict(ds: DataSource) -> dict:
    return {
        "shard_hosts": ds.shard_hosts,
        "database_server_url": ds.database_server_url,
        "schema_name": ds.schema_name,
        "region": ds.region.remote_bind_address if ds.region else None,
        "name": ds.name,
    }


@tool(
    name="locate_tenant",
    description="Find which database server and schema a tenant lives on.",
    input_schema={
        "host_name": Annotated[str, "Partial hostname to search for (e.g. 'acme')"]
    },
)
async def locate_tenant(args: dict) -> dict:
    host_name: str = args["host_name"]
    regions = load_config(str(_config_path))
    results: list[DataSource] = await asyncio.to_thread(
        Tenant().find_by_host_name, host_name, regions
    )
    if not results:
        return {
            "content": [
                {"type": "text", "text": f"No tenants found for '{host_name}'."}
            ]
        }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps([_ds_to_dict(ds) for ds in results], indent=2),
            }
        ]
    }


locate_tenant_server = create_sdk_mcp_server(
    name="locate-tenant",
    tools=[locate_tenant],
)
