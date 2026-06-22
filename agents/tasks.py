import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def run_scheduled_agent(schedule_pk: int) -> str:
    from .agent_service import run_agent
    from .models import AgentRun, AgentSchedule

    try:
        schedule = AgentSchedule.objects.select_related("agent").get(pk=schedule_pk)
    except AgentSchedule.DoesNotExist:
        logger.error("AgentSchedule %s not found", schedule_pk)
        return "schedule not found"

    if not schedule.is_active:
        return "inactive"

    logger.info("Running scheduled agent %r (schedule %s)", schedule.agent.name, schedule_pk)

    run = AgentRun.objects.create(
        agent=schedule.agent,
        schedule=schedule,
        prompt=schedule.prompt,
        status=AgentRun.RUNNING,
    )

    output_parts: list[str] = []
    try:
        for chunk in run_agent(schedule.agent, schedule.prompt):
            output_parts.append(chunk)
        run.status = AgentRun.COMPLETED
        logger.info("Scheduled agent run completed (schedule %s, run %s)", schedule_pk, run.pk)
    except Exception:
        logger.exception("Scheduled agent run failed (schedule %s, run %s)", schedule_pk, run.pk)
        run.status = AgentRun.FAILED

    run.output = "".join(output_parts)
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "output", "completed_at"])

    schedule.last_run = timezone.now()
    schedule.save(update_fields=["last_run"])
    return run.status


def run_command_run(run_pk: int) -> str:
    from ops.tasks import run_command_run as run_ops_command_run

    return run_ops_command_run(run_pk)


def run_scheduled_command(schedule_pk: int) -> str:
    from ops.tasks import run_scheduled_command as run_ops_scheduled_command

    return run_ops_scheduled_command(schedule_pk)
