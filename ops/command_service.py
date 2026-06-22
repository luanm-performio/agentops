from django.db import models
from django.db.models import OuterRef, Subquery
from django_q.models import Schedule as QSchedule
from django_q.tasks import async_task

from .models import CommandRun, CommandSchedule


class CommandScheduleService:
    @staticmethod
    def all() -> models.QuerySet[CommandSchedule]:
        latest_run = CommandRun.objects.filter(schedule=OuterRef("pk")).order_by(
            "-created_at"
        )
        return CommandSchedule.objects.annotate(
            last_run_pk=Subquery(latest_run.values("pk")[:1]),
            last_run_status=Subquery(latest_run.values("status")[:1]),
        )

    @staticmethod
    def create(schedule: CommandSchedule) -> CommandSchedule:
        if schedule.is_active:
            CommandScheduleService._sync_q(schedule)
        return schedule

    @staticmethod
    def delete(schedule: CommandSchedule) -> None:
        CommandScheduleService._remove_q(schedule)
        schedule.delete()

    @staticmethod
    def toggle(schedule: CommandSchedule) -> CommandSchedule:
        schedule.is_active = not schedule.is_active
        schedule.save(update_fields=["is_active"])
        if schedule.is_active:
            CommandScheduleService._sync_q(schedule)
        else:
            CommandScheduleService._remove_q(schedule)
        return schedule

    @staticmethod
    def _sync_q(schedule: CommandSchedule) -> None:
        defaults: dict = {
            "func": "ops.tasks.run_scheduled_command",
            "args": str(schedule.pk),
            "repeats": -1,
        }
        if schedule.schedule_type == CommandSchedule.CRON:
            defaults["schedule_type"] = QSchedule.CRON
            defaults["cron"] = schedule.cron_expression
        else:
            defaults["schedule_type"] = QSchedule.MINUTES
            defaults["minutes"] = schedule.interval_minutes

        QSchedule.objects.update_or_create(
            name=schedule.q_schedule_name(),
            defaults=defaults,
        )

    @staticmethod
    def _remove_q(schedule: CommandSchedule) -> None:
        QSchedule.objects.filter(name=schedule.q_schedule_name()).delete()


def enqueue_command_run(
    command_key: str,
    params: dict,
    schedule: CommandSchedule | None = None,
) -> CommandRun:
    run = CommandRun.objects.create(
        command_key=command_key,
        params=params,
        schedule=schedule,
        status=CommandRun.QUEUED,
    )
    async_task("ops.tasks.run_command_run", run.pk)
    return run
