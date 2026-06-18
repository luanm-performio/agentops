from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    # Locate tenant
    path("locate-tenant/", views.locate_tenant, name="locate_tenant"),
    # Agents
    path("agents/", views.agents, name="agents"),
    path("agents/create/", views.agent_create, name="agent_create"),
    path("agents/<int:pk>/edit/", views.agent_update, name="agent_update"),
    path("agents/<int:pk>/delete/", views.agent_delete, name="agent_delete"),
    path("agents/<int:pk>/run/", views.agent_run, name="agent_run"),
    path("agents/<int:pk>/run/start/", views.agent_run_start, name="agent_run_start"),
    path(
        "agents/<int:pk>/run/stream/", views.agent_run_stream, name="agent_run_stream"
    ),
    # Schedules
    path("schedules/", views.schedules, name="schedules"),
    path("schedules/create/", views.schedule_create, name="schedule_create"),
    path("schedules/<int:pk>/delete/", views.schedule_delete, name="schedule_delete"),
    path("schedules/<int:pk>/toggle/", views.schedule_toggle, name="schedule_toggle"),
    # Run history
    path("runs/", views.runs, name="runs"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("runs/<int:pk>/export.md", views.run_export_md, name="run_export_md"),
    # Chat
    path("chats/", views.chat_list, name="chat_list"),
    path("chats/new/", views.chat_new, name="chat_new"),
    path("chats/<int:pk>/", views.chat_detail, name="chat_detail"),
    path("chats/<int:pk>/send/", views.chat_send, name="chat_send"),
    path("chats/<int:pk>/stream/", views.chat_stream, name="chat_stream"),
    path("chats/<int:pk>/messages/", views.chat_messages, name="chat_messages"),
    path("chats/<int:pk>/export.md", views.chat_export_md, name="chat_export_md"),
    path("chats/<int:pk>/delete/", views.chat_delete, name="chat_delete"),
    path("chats/sessions/", views.chat_sessions_partial, name="chat_sessions_partial"),
]
