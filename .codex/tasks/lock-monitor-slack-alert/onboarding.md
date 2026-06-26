# Lock Monitor Slack Alert Onboarding

## Request

Build an alert for lock monitor runs: when a monitored database job/process runs over a threshold such as 2 hours, send a Slack alert. The user noted that an AI agent can be used to send the Slack message.

## Relevant Project Shape

- Django app with HTMX UI and Django Q background jobs.
- Lock monitor command is registered under `ops.commands.lock_monitor.COMMAND`.
- Command runs are queued through `ops.command_service.enqueue_command_run`, which creates `CommandRun` and calls `async_task("ops.tasks.run_command_run", run.pk)`.
- `ops.tasks.run_command_run` calls `run_registered_command`, which dispatches command handlers.
- Lock monitor collection lives in `ops.lock_monitor.run_lock_monitor`.
- Lock monitor records live in `LockMonitoringRecord`; MySQL process rows have `record_type="process"` and `duration_seconds`.
- Existing agent runs live in `agents.models.AgentRun`.
- Scheduled agents use `agents.tasks.run_scheduled_agent`, which invokes `agents.agent_service.run_agent`.

## Implementation Notes

- Added `agents.tasks.run_agent_prompt(agent_pk, prompt)` for ad-hoc background agent prompts.
- Added lock monitor command params:
  - `alert_threshold_seconds`, default `7200`; `0`, blank, or missing disables alerting.
  - `alert_agent_id`, optional per-command agent selector.
  - `alert_agent_name`, optional per-command fallback selector.
  - `alert_recipient`, default `me`.
- Added settings/env fallbacks:
  - `LOCK_MONITOR_ALERT_AGENT_ID`
  - `LOCK_MONITOR_ALERT_AGENT_NAME`
  - `LOCK_MONITOR_ALERT_RECIPIENT`
- Alerting is based on process records for the capture where `duration_seconds >= alert_threshold_seconds`.
- Alerting queues `async_task("agents.tasks.run_agent_prompt", agent_pk, prompt)` rather than sending Slack directly.
- If overdue records exist but no alert agent is configured, command output includes `Alert skipped...`.
- If queueing fails, the lock monitor command still completes and reports `Alert failed to queue...`; errors are logged.

## Tests Added

- Lock monitor command queues an agent alert when an overdue process is captured.
- Lock monitor command reports skipped alert when an overdue process exists but no agent is configured.
- Ad-hoc agent task creates an `AgentRun`, captures output, and completes.

## Files Touched

- `agents/tasks.py`
- `ops/commands/lock_monitor.py`
- `ops/tests.py`
- `config/settings.py`
- `README.md`
