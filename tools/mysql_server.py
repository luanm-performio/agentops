import asyncio
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Annotated, NotRequired, TypedDict

from claude_agent_sdk import create_sdk_mcp_server, tool
from sqlalchemy import Engine, text

from .db_config import load_config
from .tenant import DataSource, Tunnel

_config_path = Path(__file__).parent / "config.yaml"

# Persistent session store: session_key -> (Tunnel, Engine)
_sessions: dict[str, tuple[Tunnel, Engine]] = {}

_DESTRUCTIVE_SQL = re.compile(
    r"^\s*(DELETE|TRUNCATE|UPDATE|DROP|ALTER|INSERT|REPLACE)\b",
    re.IGNORECASE,
)


def _session_key(database_server_url: str, schema_name: str) -> str:
    raw = f"{database_server_url}|{schema_name}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


def _get_engine(session_key: str) -> Engine | None:
    entry = _sessions.get(session_key)
    return entry[1] if entry else None


def _text(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}]}


def _error(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


@tool(
    name="connect_to_database",
    description="Connect to a tenant database and return a session key for subsequent queries.",
    input_schema={
        "tenant_name": Annotated[
            str, "Shard hostnames (e.g. 'acme-shard1,acme-shard2')"
        ],
        "schema_name": Annotated[str, "Schema name (e.g. 'performancecentre_global')"],
        "database_server_url": Annotated[
            str, "DB server URL (e.g. 'db.example.com:3306')"
        ],
        "region": Annotated[str, "Region remote_bind_address or name from config"],
        "name": Annotated[str, "Human-readable session name (e.g. 'au-prod')"],
    },
)
async def connect_to_database(args: dict) -> dict:
    tenant_name = args["tenant_name"]
    schema_name = args["schema_name"]
    database_server_url = args["database_server_url"]
    region = args["region"]
    name = args["name"]

    key = _session_key(database_server_url, schema_name)

    if key in _sessions:
        _, engine = _sessions[key]
        try:
            await asyncio.to_thread(
                lambda: engine.connect().__enter__().execute(text("SELECT 1"))
            )
            return _text(
                f"Reusing session {key}: {tenant_name} at {database_server_url}/{schema_name}."
            )
        except Exception:
            _sessions.pop(key)[0].__exit__(None, None, None)

    regions = load_config(str(_config_path))
    matched_region = next(
        (r for r in regions if r.remote_bind_address == region or r.name == name), None
    )
    if not matched_region:
        return _error(f"Region '{region}' not found in config.")

    custom_region = replace(matched_region, remote_bind_address=database_server_url)
    data_source = DataSource(
        username=custom_region.username,
        password=custom_region.password,
        database_server_url=database_server_url,
        schema_name=schema_name,
    )
    tunnel = Tunnel(data_source, custom_region)

    def _connect() -> Engine:
        engine = tunnel.__enter__()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine

    try:
        engine = await asyncio.to_thread(_connect)
    except Exception as exc:
        tunnel.__exit__(None, None, None)
        return _error(
            f"Connection failed for {database_server_url}. Check VPN. Error: {exc}"
        )

    _sessions[key] = (tunnel, engine)
    return _text(
        f"Connected session {key}: {tenant_name} at {database_server_url}/{schema_name}."
    )


@tool(
    name="disconnect",
    description="Dispose of a single persisted database session.",
    input_schema={
        "session_key": Annotated[str, "Session key returned by connect_to_database"]
    },
)
async def disconnect(args: dict) -> dict:
    key = args["session_key"]
    if key not in _sessions:
        return _error(f"Session '{key}' not found. Active: {list(_sessions.keys())}")
    tunnel, _ = _sessions.pop(key)
    tunnel.__exit__(None, None, None)
    return _text(f"Session {key} disconnected.")


@tool(
    name="list_tables",
    description="Return all table names in the connected schema.",
    input_schema={
        "session_key": Annotated[str, "Session key from connect_to_database"]
    },
)
async def list_tables(args: dict) -> dict:
    engine = _get_engine(args["session_key"])
    if engine is None:
        return _error(
            f"Session '{args['session_key']}' not found. Call connect_to_database first."
        )

    def _run() -> list[str]:
        with engine.connect() as conn:
            return [row[0] for row in conn.execute(text("SHOW TABLES")).fetchall()]

    tables = await asyncio.to_thread(_run)
    return _text("\n".join(tables))


@tool(
    name="describe_table",
    description="Return column definitions (Field, Type, Null, Key, Default, Extra) for a table.",
    input_schema={
        "session_key": Annotated[str, "Session key from connect_to_database"],
        "table_name": Annotated[str, "Table name to inspect"],
    },
)
async def describe_table(args: dict) -> dict:
    engine = _get_engine(args["session_key"])
    if engine is None:
        return _error(
            f"Session '{args['session_key']}' not found. Call connect_to_database first."
        )

    table_name = args["table_name"]

    def _run() -> list[dict]:
        with engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(text(f"DESCRIBE `{table_name}`"))
                .mappings()
                .fetchall()
            ]

    rows = await asyncio.to_thread(_run)
    return _text(json.dumps(rows, indent=2))


class ExecuteQueryInput(TypedDict):
    session_key: str
    sql: str
    limit: NotRequired[int]
    confirm: NotRequired[bool]


@tool(
    name="execute_query",
    description=(
        "Execute a SQL statement and return up to `limit` rows (default 100). "
        "Destructive statements (DELETE, TRUNCATE, UPDATE, DROP, ALTER, INSERT, REPLACE) "
        "require confirm=true — ask the user first, then re-call with confirm=true."
    ),
    input_schema=ExecuteQueryInput,
)
async def execute_query(args: dict) -> dict:
    session_key = args["session_key"]
    sql = args["sql"]
    limit: int = args.get("limit", 100)
    confirm: bool = args.get("confirm", False)

    if _DESTRUCTIVE_SQL.match(sql) and not confirm:
        return _error(
            f"'{sql.split()[0].upper()}' is a destructive operation. "
            "Ask the user to confirm, then re-call with confirm=true."
        )

    engine = _get_engine(session_key)
    if engine is None:
        return _error(
            f"Session '{session_key}' not found. Call connect_to_database first."
        )

    def _run() -> list[dict]:
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            return [dict(r) for r in result.mappings().fetchmany(limit)]

    try:
        rows = await asyncio.to_thread(_run)
    except Exception as exc:
        return _error(f"Query failed: {exc}")

    return _text(json.dumps({"query": sql, "rows": rows}, indent=2, default=str))


@tool(
    name="disconnect_all",
    description="Dispose of all persisted database sessions.",
    input_schema={},
)
async def disconnect_all(args: dict) -> dict:
    keys = list(_sessions.keys())
    for key in keys:
        tunnel, _ = _sessions.pop(key)
        tunnel.__exit__(None, None, None)
    return _text(f"Disconnected {len(keys)} session(s): {keys}")


mysql_server = create_sdk_mcp_server(
    name="mysql-mcp-server",
    tools=[
        connect_to_database,
        disconnect,
        list_tables,
        describe_table,
        execute_query,
        disconnect_all,
    ],
)
