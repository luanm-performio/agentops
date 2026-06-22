from django.urls import path

from . import views

urlpatterns = [
    path("calc-log/", views.calc_log_dashboards, name="calc_log_dashboards"),
    path(
        "calc-log/<int:pk>/",
        views.calc_log_dashboard_detail,
        name="calc_log_dashboard_detail",
    ),
    path(
        "calc-log/<int:pk>/dashboard/",
        views.calc_log_dashboard_html,
        name="calc_log_dashboard_html",
    ),
    path(
        "calc-log/<int:pk>/delete/",
        views.calc_log_dashboard_delete,
        name="calc_log_dashboard_delete",
    ),
    path("locate-tenant/", views.locate_tenant, name="locate_tenant"),
    path("lock-monitor/", views.lock_monitor, name="lock_monitor"),
    path(
        "lock-monitor/results/",
        views.lock_monitor_results,
        name="lock_monitor_results",
    ),
    path(
        "lock-monitor/export.xlsx",
        views.lock_monitor_export,
        name="lock_monitor_export",
    ),
    path("squelch/", views.squelch, name="squelch"),
    path("squelch/export.xlsx", views.squelch_export, name="squelch_export"),
    path("commands/", views.commands, name="commands"),
    path("commands/run/", views.command_run_create, name="command_run_create"),
    path(
        "commands/schedule/",
        views.command_schedule_create,
        name="command_schedule_create",
    ),
    path(
        "commands/schedules/<int:pk>/toggle/",
        views.command_schedule_toggle,
        name="command_schedule_toggle",
    ),
    path(
        "commands/schedules/<int:pk>/delete/",
        views.command_schedule_delete,
        name="command_schedule_delete",
    ),
    path(
        "commands/runs/<int:pk>/", views.command_run_detail, name="command_run_detail"
    ),
]
