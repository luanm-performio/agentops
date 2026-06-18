# Lightweight registry — safe to import anywhere (no heavy deps).
MCP_CHOICES: list[tuple[str, str]] = [
    ("locate-tenant", "Locate Tenant"),
    ("mysql", "MySQL"),
]


def get_mcp_servers(server_names: list[str]) -> dict:
    """Return SDK server configs for the requested names, importing lazily."""
    if not server_names:
        return {}

    result = {}
    if "locate-tenant" in server_names:
        from .server import locate_tenant_server  # noqa: PLC0415

        result["locate-tenant"] = locate_tenant_server
    if "mysql" in server_names:
        from .mysql_server import mysql_server  # noqa: PLC0415

        result["mysql"] = mysql_server
    return result


__all__ = ["MCP_CHOICES", "get_mcp_servers"]
