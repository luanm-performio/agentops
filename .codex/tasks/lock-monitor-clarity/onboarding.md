# Lock Monitor Clarity Onboarding

## Request

Improve lock monitor clarity because searching for locks makes it hard to spot what is wrong when calculations or queries are slow.

## Approach

- Keep the collector unchanged for now.
- Add a view-level diagnosis layer from existing records and signals.
- Add UI sections that make the capture scannable before raw tables:
  - likely issue
  - next step
  - long-running count
  - lock wait count
  - top suspects

## Implementation Notes

- Added `_lock_monitor_diagnosis` in `ops/views.py`.
- Added `_lock_monitor_top_suspects`, scoring, severity, reason, and signal label helpers.
- Updated `ops/templates/partials/_lock_monitor_results.html` to show the diagnosis card before warnings/raw sections.
- Added tests for:
  - lock contention diagnosis
  - no-lock slow query diagnosis

## Files Touched

- `ops/views.py`
- `ops/templates/partials/_lock_monitor_results.html`
- `ops/tests.py`
