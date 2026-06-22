from django.contrib import admin

from .models import (
    CalcLogDashboard,
    CommandRun,
    CommandSchedule,
    LockMonitorCapture,
    LockMonitoringRecord,
)


@admin.register(CalcLogDashboard)
class CalcLogDashboardAdmin(admin.ModelAdmin):
    list_display = ("name", "source_folder", "created_at")
    search_fields = ("name", "source_folder")
    readonly_fields = ("source_files", "html_content", "created_at")


@admin.register(CommandSchedule)
class CommandScheduleAdmin(admin.ModelAdmin):
    list_display = ("command_key", "schedule_type", "is_active", "last_run")
    list_filter = ("schedule_type", "is_active")
    search_fields = ("command_key",)


@admin.register(CommandRun)
class CommandRunAdmin(admin.ModelAdmin):
    list_display = ("command_key", "status", "created_at", "started_at", "completed_at")
    list_filter = ("status",)
    search_fields = ("command_key",)


@admin.register(LockMonitorCapture)
class LockMonitorCaptureAdmin(admin.ModelAdmin):
    list_display = (
        "tenant_host",
        "status",
        "data_source_count",
        "process_count",
        "lock_count",
        "started_at",
    )
    list_filter = ("status",)
    search_fields = ("tenant_host",)


@admin.register(LockMonitoringRecord)
class LockMonitoringRecordAdmin(admin.ModelAdmin):
    list_display = (
        "record_type",
        "schema_name",
        "process_id",
        "duration_seconds",
        "captured_at",
    )
    list_filter = ("record_type", "region")
    search_fields = (
        "capture__tenant_host",
        "schema_name",
        "query_text",
        "blocking_query_text",
        "explanation",
        "explain_error",
    )
