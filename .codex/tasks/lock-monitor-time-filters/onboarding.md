Task: Add Lock Monitor date/time filters

User request:
- In the Lock Monitor menu, add filters for a from time and to time on a certain date so fullcalc events can be found.

Relevant local guidance read:
- `.agents/skills/onboard/SKILL.md`
- `.agents/skills/htmx-patterns/SKILL.md`
- `.agents/skills/django-templates/SKILL.md`
- `.agents/skills/pytest-django-patterns/SKILL.md`

Code paths:
- `ops/views.py`
  - `lock_monitor()` renders the main Lock Monitor page and partial.
  - `lock_monitor_results()` renders `partials/_lock_monitor_results.html`.
  - `lock_monitor_export()` exports `_filtered_lock_monitor_records()`.
  - `_lock_monitor_context()` builds captures, records, pagination, and template filter values.
  - `_filtered_lock_monitor_records()` applies existing record-level filters.
- `ops/templates/partials/_lock_monitor.html`
  - Contains the HTMX filter form with `hx-get` to `lock_monitor_results`.
  - Existing filters: run ID, tenant host, schema, record type, search.
  - Export button serializes this form, so adding inputs here should automatically affect XLSX export if the shared helper filters records.
- `ops/templates/partials/_lock_monitor_results.html`
  - Refreshes results every 10 seconds and preserves `filter_query`.
- `ops/models.py`
  - `LockMonitoringRecord.captured_at` is a timezone-aware `DateTimeField(auto_now_add=True, db_index=True)`.
  - `LockMonitorCapture.started_at` is used for snapshot display and capture ordering.
- `ops/tests.py`
  - `LockMonitorViewTests` covers page controls, result filtering, capture pagination, and export.

Implementation notes:
- Use `captured_at` for the new date/time record filter because the user wants to find captured events.
- Add date, from time, and to time GET parameters to the filter form.
- Use Django timezone utilities and date parsing to build local Australia/Sydney day bounds.
- Preserve invalid date/time text in the inputs but ignore invalid bounds.
- Make capture pagination narrow to captures with matching records whenever record-level filters are active, otherwise a matching older event could remain hidden on a later capture page.

Verification plan:
- Add focused tests before production changes.
- Run the focused Lock Monitor view tests with `uv run python manage.py test ops.tests.LockMonitorViewTests`.
