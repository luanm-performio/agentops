import html as html_lib
import logging
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .agent_service import run_agent
from .forms import AgentForm, AgentScheduleForm
from .models import Agent, AgentRun, AgentSchedule, ChatMessage, ChatSession
from .schedule_service import ScheduleService
from .tenant_lookup import TenantLookupResult, locate_tenants

_PROMPT_SESSION_KEY = "agent_run_prompt_{pk}"
logger = logging.getLogger(__name__)


# ── Dashboard ──────────────────────────────────────────────────────────────


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    if request.headers.get("HX-Request"):
        return render(request, "partials/_dashboard.html")
    return render(request, "dashboard.html")


# ── Locate tenant ──────────────────────────────────────────────────────────


@login_required
@require_http_methods(["GET", "POST"])
def locate_tenant(request: HttpRequest) -> HttpResponse:
    host_name = ""
    results: list[TenantLookupResult] = []
    error = ""
    has_searched = False
    status = 200

    if request.method == "POST":
        host_name = request.POST.get("host_name", "").strip()
        has_searched = True

        if not host_name:
            error = "Enter a host name to search."
            status = 422
        else:
            try:
                results = locate_tenants(host_name)
            except Exception:
                logger.exception("Tenant lookup failed for host_name=%s", host_name)
                error = "Tenant lookup failed. Check the server logs for details."
                status = 500

    context = {
        "host_name": host_name,
        "results": results,
        "error": error,
        "has_searched": has_searched,
    }
    if request.headers.get("HX-Request"):
        return render(request, "partials/_locate_tenant.html", context, status=status)
    return render(request, "locate_tenant.html", context, status=status)


# ── Agents ─────────────────────────────────────────────────────────────────


@login_required
def agents(request: HttpRequest) -> HttpResponse:
    context = {"agents": Agent.objects.all()}
    if request.headers.get("HX-Request"):
        return render(request, "partials/_agents.html", context)
    return render(request, "agents.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def agent_create(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        response = render(
            request,
            "partials/_agent_form.html",
            {
                "form": AgentForm(),
                "form_action": reverse("agent_create"),
                "modal_title": "Create Agent",
            },
        )
        response["HX-Trigger"] = "openModal"
        return response

    form = AgentForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "partials/_agent_form.html",
            {
                "form": form,
                "form_action": reverse("agent_create"),
                "modal_title": "Create Agent",
            },
            status=422,
        )

    form.save()
    response = render(
        request, "partials/_agent_list.html", {"agents": Agent.objects.all()}
    )
    response["HX-Retarget"] = "#agent-list"
    response["HX-Reswap"] = "innerHTML"
    response["HX-Trigger"] = "closeModal"
    return response


@login_required
@require_POST
def agent_delete(request: HttpRequest, pk: int) -> HttpResponse:
    get_object_or_404(Agent, pk=pk).delete()
    response = render(
        request, "partials/_agent_list.html", {"agents": Agent.objects.all()}
    )
    response["HX-Retarget"] = "#agent-list"
    response["HX-Reswap"] = "innerHTML"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def agent_update(request: HttpRequest, pk: int) -> HttpResponse:
    agent = get_object_or_404(Agent, pk=pk)

    if request.method == "GET":
        response = render(
            request,
            "partials/_agent_form.html",
            {
                "form": AgentForm(instance=agent),
                "form_action": reverse("agent_update", args=[pk]),
                "modal_title": "Edit Agent",
            },
        )
        response["HX-Trigger"] = "openModal"
        return response

    form = AgentForm(request.POST, instance=agent)
    if not form.is_valid():
        return render(
            request,
            "partials/_agent_form.html",
            {
                "form": form,
                "form_action": reverse("agent_update", args=[pk]),
                "modal_title": "Edit Agent",
            },
            status=422,
        )

    form.save()
    response = render(
        request, "partials/_agent_list.html", {"agents": Agent.objects.all()}
    )
    response["HX-Retarget"] = "#agent-list"
    response["HX-Reswap"] = "innerHTML"
    response["HX-Trigger"] = "closeModal"
    return response


# ── Agent run ──────────────────────────────────────────────────────────────


@login_required
@require_GET
def agent_run(request: HttpRequest, pk: int) -> HttpResponse:
    agent = get_object_or_404(Agent, pk=pk)
    response = render(request, "partials/_agent_run_form.html", {"agent": agent})
    response["HX-Trigger"] = "openRunModal"
    return response


@login_required
def agent_run_start(request: HttpRequest, pk: int) -> HttpResponse:
    agent = get_object_or_404(Agent, pk=pk)
    prompt = request.POST.get("prompt", "").strip()

    if not prompt:
        return render(
            request,
            "partials/_agent_run_form.html",
            {
                "agent": agent,
                "error": "Please enter a task for the agent.",
            },
            status=422,
        )

    request.session[_PROMPT_SESSION_KEY.format(pk=pk)] = prompt
    request.session.modified = True
    return render(
        request, "partials/_agent_run_output.html", {"agent": agent, "prompt": prompt}
    )


@login_required
@require_GET
def agent_run_stream(request: HttpRequest, pk: int) -> StreamingHttpResponse:
    agent = get_object_or_404(Agent, pk=pk)
    prompt = request.session.get(_PROMPT_SESSION_KEY.format(pk=pk), "")

    def _sse(event: str, data: str) -> str:
        lines = "\n".join(f"data: {line}" for line in data.splitlines())
        return f"event: {event}\n{lines}\n\n"

    def sse_events():
        from django.utils import timezone

        run = AgentRun.objects.create(
            agent=agent, prompt=prompt, status=AgentRun.RUNNING
        )
        output_parts: list[str] = []
        try:
            for chunk in run_agent(agent, prompt):
                output_parts.append(chunk)
                yield _sse("message", chunk)
            run.status = AgentRun.COMPLETED
        except Exception as exc:
            err_html = f'<p class="output-error">{html_lib.escape(str(exc))}</p>'
            output_parts.append(err_html)
            yield _sse("message", err_html)
            run.status = AgentRun.FAILED
        finally:
            run.output = "".join(output_parts)
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "output", "completed_at"])
            yield "event: close\ndata: {}\n\n"

    response = StreamingHttpResponse(sse_events(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


# ── Schedules ──────────────────────────────────────────────────────────────


@login_required
def schedules(request: HttpRequest) -> HttpResponse:
    context = {"schedules": ScheduleService.all()}
    if request.headers.get("HX-Request"):
        return render(request, "partials/_schedules.html", context)
    return render(request, "schedules.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def schedule_create(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        response = render(
            request,
            "partials/_schedule_form.html",
            {
                "form": AgentScheduleForm(),
                "form_action": reverse("schedule_create"),
                "modal_title": "New Schedule",
            },
        )
        response["HX-Trigger"] = "openModal"
        return response

    form = AgentScheduleForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "partials/_schedule_form.html",
            {
                "form": form,
                "form_action": reverse("schedule_create"),
                "modal_title": "New Schedule",
            },
            status=422,
        )

    ScheduleService.create(form.save())
    response = render(
        request, "partials/_schedule_list.html", {"schedules": ScheduleService.all()}
    )
    response["HX-Retarget"] = "#schedule-list"
    response["HX-Reswap"] = "innerHTML"
    response["HX-Trigger"] = "closeModal"
    return response


@login_required
@require_POST
def schedule_delete(request: HttpRequest, pk: int) -> HttpResponse:
    ScheduleService.delete(get_object_or_404(AgentSchedule, pk=pk))
    response = render(
        request, "partials/_schedule_list.html", {"schedules": ScheduleService.all()}
    )
    response["HX-Retarget"] = "#schedule-list"
    response["HX-Reswap"] = "innerHTML"
    return response


@login_required
@require_POST
def schedule_toggle(request: HttpRequest, pk: int) -> HttpResponse:
    ScheduleService.toggle(get_object_or_404(AgentSchedule, pk=pk))
    response = render(
        request, "partials/_schedule_list.html", {"schedules": ScheduleService.all()}
    )
    response["HX-Retarget"] = "#schedule-list"
    response["HX-Reswap"] = "innerHTML"
    return response


# ── Run history ────────────────────────────────────────────────────────────

_RUNS_PAGE_SIZE = 25


@login_required
def runs(request: HttpRequest) -> HttpResponse:
    qs = AgentRun.objects.select_related("agent", "schedule")

    agent_id = request.GET.get("agent", "").strip()
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()

    if agent_id:
        qs = qs.filter(agent_id=agent_id)
    if date_from:
        qs = qs.filter(started_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(started_at__date__lte=date_to)

    paginator = Paginator(qs, _RUNS_PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page", 1))

    filter_params: dict[str, str] = {}
    if agent_id:
        filter_params["agent"] = agent_id
    if date_from:
        filter_params["date_from"] = date_from
    if date_to:
        filter_params["date_to"] = date_to

    context = {
        "page": page,
        "agents": Agent.objects.order_by("name"),
        "current_agent": agent_id,
        "current_date_from": date_from,
        "current_date_to": date_to,
        "filter_qs": urlencode(filter_params),
        "is_filtered": bool(agent_id or date_from or date_to),
    }
    if request.headers.get("HX-Request"):
        return render(request, "partials/_runs.html", context)
    return render(request, "runs.html", context)


@login_required
@require_GET
def run_detail(request: HttpRequest, pk: int) -> HttpResponse:
    run = get_object_or_404(AgentRun.objects.select_related("agent", "schedule"), pk=pk)
    if request.headers.get("HX-Request"):
        return render(request, "partials/_run_detail.html", {"run": run})
    return render(request, "run_detail.html", {"run": run})


@login_required
@require_GET
def run_export_md(request: HttpRequest, pk: int) -> HttpResponse:
    import html2text

    run = get_object_or_404(AgentRun.objects.select_related("agent", "schedule"), pk=pk)

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.body_width = 0
    md_body = h.handle(run.output) if run.output else "_No output recorded._"

    trigger = "schedule" if run.schedule else "manual"
    schedule_line = (
        f"**Schedule:** {run.schedule.schedule_display}  \n" if run.schedule else ""
    )
    duration_line = (
        f"{run.duration_seconds}s" if run.duration_seconds is not None else "—"
    )

    content = (
        f"# {run.agent.name} — Run #{run.pk}\n\n"
        f"**Status:** {run.get_status_display()}  \n"
        f"**Triggered by:** {trigger}  \n"
        f"{schedule_line}"
        f"**Started:** {run.started_at.strftime('%Y-%m-%d %H:%M:%S')} UTC  \n"
        f"**Duration:** {duration_line}\n\n"
        f"## Prompt\n\n"
        f"{run.prompt}\n\n"
        f"## Output\n\n"
        f"{md_body}"
    )

    slug = run.agent.name.lower().replace(" ", "-")
    filename = f"run-{run.pk}-{slug}.md"
    response = HttpResponse(content, content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ── Chat ───────────────────────────────────────────────────────────────────


def _chat_sse(event: str, data: str) -> str:
    lines = data.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    encoded = "\n".join(f"data: {line}" for line in lines)
    return f"event: {event}\n{encoded}\n\n"


@login_required
def chat_list(request: HttpRequest) -> HttpResponse:
    sessions = ChatSession.objects.select_related("agent").all()
    first = sessions.first()
    if first:
        return redirect("chat_detail", pk=first.pk)
    return render(
        request,
        "chat.html",
        {
            "sessions": sessions,
            "session": None,
            "agents": Agent.objects.order_by("name"),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def chat_new(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        return render(
            request,
            "partials/_chat_new_form.html",
            {
                "agents": Agent.objects.order_by("name"),
            },
        )

    agent_id = request.POST.get("agent_id") or None
    agent = Agent.objects.filter(pk=agent_id).first() if agent_id else None
    session = ChatSession.objects.create(agent=agent, title="")
    response = HttpResponse(status=204)
    response["HX-Redirect"] = reverse("chat_detail", args=[session.pk])
    return response


@login_required
def chat_detail(request: HttpRequest, pk: int) -> HttpResponse:
    session = get_object_or_404(ChatSession.objects.select_related("agent"), pk=pk)
    sessions = ChatSession.objects.select_related("agent").all()
    context = {
        "session": session,
        "messages": session.messages.order_by("created_at"),
        "sessions": sessions,
        "agents": Agent.objects.order_by("name"),
    }
    if request.headers.get("HX-Request"):
        return render(request, "partials/_chat_layout.html", context)
    return render(request, "chat.html", context)


@login_required
@require_POST
def chat_send(request: HttpRequest, pk: int) -> HttpResponse:
    session = get_object_or_404(ChatSession, pk=pk)
    content = request.POST.get("content", "").strip()
    if not content:
        return HttpResponse(status=400)

    ChatMessage.objects.create(session=session, role=ChatMessage.USER, content=content)

    if not session.title:
        session.title = content[:60] + ("…" if len(content) > 60 else "")
        session.save(update_fields=["title"])

    return render(
        request,
        "partials/_chat_send_ack.html",
        {
            "session": session,
            "content": content,
        },
    )


@login_required
@require_GET
def chat_stream(request: HttpRequest, pk: int) -> StreamingHttpResponse:
    from django.utils import timezone

    session = get_object_or_404(ChatSession.objects.select_related("agent"), pk=pk)

    def sse_events():
        from .chat_service import stream_reply

        msgs = list(session.messages.order_by("created_at"))
        if not msgs or msgs[-1].role != ChatMessage.USER:
            yield _chat_sse("error", "No pending user message.")
            return

        parts: list[str] = []
        try:
            for kind, value in stream_reply(session):
                if kind == "token":
                    parts.append(value)
                    yield _chat_sse("token", value)
                elif kind == "error":
                    yield _chat_sse("error", value)
                    return

            full_text = "".join(parts)
            ChatMessage.objects.create(
                session=session, role=ChatMessage.ASSISTANT, content=full_text
            )
            ChatSession.objects.filter(pk=session.pk).update(updated_at=timezone.now())
            yield _chat_sse("done", str(session.pk))
        except Exception as exc:
            yield _chat_sse("error", str(exc))

    response = StreamingHttpResponse(sse_events(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@login_required
@require_GET
def chat_messages(request: HttpRequest, pk: int) -> HttpResponse:
    session = get_object_or_404(ChatSession, pk=pk)
    return render(
        request,
        "partials/_chat_messages.html",
        {
            "session": session,
            "messages": session.messages.order_by("created_at"),
        },
    )


@login_required
@require_GET
def chat_export_md(request: HttpRequest, pk: int) -> HttpResponse:
    session = get_object_or_404(ChatSession.objects.select_related("agent"), pk=pk)
    msgs = session.messages.order_by("created_at")

    lines: list[str] = [
        f"# {session.title or f'Chat #{session.pk}'}",
        "",
        f"**Agent:** {session.agent.name if session.agent else '—'}  ",
        f"**Started:** {session.created_at.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        "---",
        "",
    ]
    for msg in msgs:
        role_label = "**You**" if msg.role == ChatMessage.USER else "**Assistant**"
        lines.append(f"{role_label}  ")
        lines.append(msg.content)
        lines.append("")
        lines.append("---")
        lines.append("")

    content = "\n".join(lines)
    slug = (session.title or f"chat-{session.pk}").lower().replace(" ", "-")[:40]
    response = HttpResponse(content, content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{slug}.md"'
    return response


@login_required
@require_GET
def chat_sessions_partial(request: HttpRequest) -> HttpResponse:
    active_pk = request.GET.get("active")
    sessions = ChatSession.objects.select_related("agent").all()
    session = ChatSession.objects.filter(pk=active_pk).first() if active_pk else None
    return render(
        request,
        "partials/_chat_sessions.html",
        {
            "sessions": sessions,
            "session": session,
        },
    )


@login_required
@require_POST
def chat_delete(request: HttpRequest, pk: int) -> HttpResponse:
    session = get_object_or_404(ChatSession, pk=pk)
    session.delete()
    # Return updated session list so HTMX can refresh sidebar
    sessions = ChatSession.objects.select_related("agent").all()
    first = sessions.first()
    if first:
        response = HttpResponse(status=204)
        response["HX-Redirect"] = reverse("chat_detail", args=[first.pk])
    else:
        response = HttpResponse(status=204)
        response["HX-Redirect"] = reverse("chat_list")
    return response
