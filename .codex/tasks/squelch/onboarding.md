# Squelch Onboarding

## Task

Build a page/menu similar to Locate Tenant, called Squelch.

Requested UI:

- Left sidebar menu item.
- Right pane with a textarea for a query.
- Regions as multi-select, using names from `tools/config.yaml` such as `au-prod` and `us-prod`.
- Search button.
- The user plans to call `squelch.py`.
- Results should be table-like in the UI rather than requiring Excel to inspect.

## Existing Context

- Main layout: `agents/templates/base.html`
- Existing Locate Tenant pattern:
  - URL: `agents/urls.py`
  - View: `agents/views.py`
  - Full template: `agents/templates/locate_tenant.html`
  - Partial: `agents/templates/partials/_locate_tenant.html`
  - Service adapter: `agents/tenant_lookup.py`
- `tools/config.yaml` currently contains loaded regions:
  - `au-prod`
  - `us-prod`
  - `local`
- `tools/squelch.py` does not exist yet.
- There is an empty `agents/templates/partials/squelch.html` stub.

## Implementation Decision

Since `tools/squelch.py` is not present yet, add a Django-side adapter that imports `tools.squelch.run_squelch` lazily. That keeps the page testable and gives the future script a simple contract:

```python
def run_squelch(query: str, regions: list[str]) -> list[dict[str, object]]:
    ...
```

The UI renders returned dictionaries as a table. This is more immediately useful in-browser than returning an Excel file. Excel/CSV export can be added once the result shape is finalized.

## Validation

- Query is required.
- At least one region is required.
- Region choices come from config and exclude `local`.
- Runtime errors are logged and shown as user-friendly messages.
