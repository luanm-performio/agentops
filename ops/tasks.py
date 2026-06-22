import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def run_command_run(run_pk: int) -> str:
    from .command_registry import run_registered_command
    from .models import CommandRun

    try:
        run = CommandRun.objects.get(pk=run_pk)
    except CommandRun.DoesNotExist:
        logger.error("CommandRun %s not found", run_pk)
        return "run not found"

    if run.status not in {CommandRun.QUEUED, CommandRun.FAILED}:
        return run.status

    logger.info("Running command %s (run %s)", run.command_key, run.pk)
    run.status = CommandRun.RUNNING
    run.started_at = timezone.now()
    run.error = ""
    run.save(update_fields=["status", "started_at", "error"])

    try:
        run.output = run_registered_command(
            run.command_key,
            run.params,
            command_run_id=run.pk,
        )
        run.status = CommandRun.COMPLETED
        logger.info("Command run completed (run %s)", run.pk)
    except Exception as exc:
        logger.exception("Command run failed (run %s)", run.pk)
        run.error = str(exc)
        run.status = CommandRun.FAILED

    run.completed_at = timezone.now()
    run.save(update_fields=["status", "output", "error", "completed_at"])
    return run.status


def run_scheduled_command(schedule_pk: int) -> str:
    from .command_service import enqueue_command_run
    from .models import CommandSchedule

    try:
        schedule = CommandSchedule.objects.get(pk=schedule_pk)
    except CommandSchedule.DoesNotExist:
        logger.error("CommandSchedule %s not found", schedule_pk)
        return "schedule not found"

    if not schedule.is_active:
        return "inactive"

    run = enqueue_command_run(
        command_key=schedule.command_key,
        params=schedule.params,
        schedule=schedule,
    )
    schedule.last_run = timezone.now()
    schedule.save(update_fields=["last_run"])
    return f"queued run {run.pk}"
