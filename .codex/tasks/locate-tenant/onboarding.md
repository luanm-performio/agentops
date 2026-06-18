# Locate Tenant Onboarding

## Task

Build a non-AI tenant lookup screen in the Django app. The user asked for another link in the left pane, a simple text box in the right pane where a user enters a host name, and lookup behavior that connects through `tools/tenant.py`.

## Relevant Project Shape

- Django app: `agents`
- Main layout and sidebar navigation: `agents/templates/base.html`
- HTMX navigation swaps content into `#content`
- Existing page pattern:
  - Full page template extends `base.html`
  - Full page includes `partials/_*.html`
  - View returns the partial when `HX-Request` is present
- URLconf: `agents/urls.py`
- Main views: `agents/views.py`
- Tenant lookup logic:
  - `tools/tenant.py` defines `Tenant.find_by_host_name(host_name, regions)`
  - `tools/db_config.py` defines `load_config`
  - `tools/config.yaml` is the default region config
  - `tools/server.py` already wraps the lookup for MCP and hides credentials in its response

## Design Notes

- Do not involve the agent/AI pipeline.
- Keep credentials out of the UI. Display only shard hosts, database server URL, schema, region, and name.
- The lookup can require external database/VPN/SSH access at runtime, so tests should mock the service function rather than hit real infrastructure.
- `tools/tenant.py` uses sibling absolute imports such as `from db_config import Region`; a Django service wrapper should add `tools/` to `sys.path` before importing those modules.

## Implementation Plan

- Add `agents/tenant_lookup.py` as a thin, testable wrapper around `tools/tenant.py`.
- Add `locate_tenant` view with explicit `GET`/`POST` handling.
- Add `agents/templates/locate_tenant.html` and `agents/templates/partials/_locate_tenant.html`.
- Add `locate-tenant/` URL.
- Add sidebar navigation entry.
- Add focused tests for GET rendering, required hostname validation, successful mocked results, and lookup failures.
