from django.db import models
from django.db.models import OuterRef, Subquery
from django_q.models import Schedule as QSchedule

from .models import AgentRun, AgentSchedule


class ScheduleService:
    """Owns all business logic for AgentSchedule lifecycle and django_q2 sync."""

    @staticmethod
    def all() -> models.QuerySet[AgentSchedule]:
        latest_run = AgentRun.objects.filter(schedule=OuterRef("pk")).order_by("-started_at")
        return AgentSchedule.objects.select_related("agent").annotate(
            last_run_pk=Subquery(latest_run.values("pk")[:1]),
            last_run_status=Subquery(latest_run.values("status")[:1]),
        )

    @staticmethod
    def create(schedule: AgentSchedule) -> AgentSchedule:
        """Persist a new schedule and register it with django_q2 if active."""
        if schedule.is_active:
            ScheduleService._sync_q(schedule)
        return schedule

    @staticmethod
    def delete(schedule: AgentSchedule) -> None:
        """Remove the django_q2 job then delete the schedule record."""
        ScheduleService._remove_q(schedule)
        schedule.delete()

    @staticmethod
    def toggle(schedule: AgentSchedule) -> AgentSchedule:
        """Flip is_active and add/remove the django_q2 job accordingly."""
        schedule.is_active = not schedule.is_active
        schedule.save(update_fields=["is_active"])
        if schedule.is_active:
            ScheduleService._sync_q(schedule)
        else:
            ScheduleService._remove_q(schedule)
        return schedule

    # ── Internal helpers ───────────────────────────────────────────────────

    @staticmethod
    def _sync_q(schedule: AgentSchedule) -> None:
        name = schedule.q_schedule_name()
        defaults: dict = {
            "func": "agents.tasks.run_scheduled_agent",
            "args": str(schedule.pk),
            "repeats": -1,
        }
        if schedule.schedule_type == AgentSchedule.CRON:
            defaults["schedule_type"] = QSchedule.CRON
            defaults["cron"] = schedule.cron_expression
        else:
            defaults["schedule_type"] = QSchedule.MINUTES
            defaults["minutes"] = schedule.interval_minutes

        QSchedule.objects.update_or_create(name=name, defaults=defaults)

    @staticmethod
    def _remove_q(schedule: AgentSchedule) -> None:
        QSchedule.objects.filter(name=schedule.q_schedule_name()).delete()
