# Edit Command Schedule Onboarding

## Request

The user wants to edit existing command schedules from the schedules table.

## Relevant Project Shape

- Command schedules are shown in `ops/templates/partials/_command_schedule_list.html`.
- New schedules are created through `ops.views.command_schedule_create`.
- The create form is `CommandScheduleForm` and is rendered in `ops/templates/partials/_command_schedule_form.html`.
- Schedules sync to Django Q through `CommandScheduleService._sync_q`.
- Existing actions are pause/resume and delete.

## Implementation Notes

- Added `CommandScheduleService.update(schedule)` to resync or remove the Django Q schedule depending on `is_active`.
- Added `command_schedule_update` GET/POST view.
- Added URL name `command_schedule_update` at `/commands/schedules/<pk>/edit/`.
- Added an `Edit` button to the schedules table that opens the existing modal with HTMX.
- Adjusted `CommandScheduleForm.__init__` so editing an existing instance uses `instance.command_key` and `instance.params`.
- Added tests for the edit action, edit modal prefill, saving/resyncing, and validation errors.

## Files Touched

- `ops/command_service.py`
- `ops/forms.py`
- `ops/urls.py`
- `ops/views.py`
- `ops/templates/partials/_command_schedule_list.html`
- `ops/tests.py`
