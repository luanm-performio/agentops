import asyncio
import queue
import threading
from collections.abc import Generator

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from .models import ChatMessage, ChatSession


_HISTORY_WINDOW = 10   # max prior messages to include
_SNIPPET_CHARS  = 400  # truncate each history message beyond this


def _build_prompt(messages: list[ChatMessage]) -> str:
    """
    Build a token-efficient prompt that includes conversation context.

    Only the most recent _HISTORY_WINDOW prior messages are included.
    Each is capped at _SNIPPET_CHARS characters so long assistant
    responses don't bloat the context. The current (last) user message
    is always included verbatim.
    """
    if len(messages) == 1:
        return messages[0].content

    history = messages[:-1]
    dropped = max(0, len(history) - _HISTORY_WINDOW)
    history = history[-_HISTORY_WINDOW:]

    lines: list[str] = []
    if dropped:
        lines.append(f"[{dropped} earlier message(s) not shown]")

    for msg in history:
        role = "U" if msg.role == ChatMessage.USER else "A"
        text = msg.content
        if len(text) > _SNIPPET_CHARS:
            text = text[:_SNIPPET_CHARS] + "…"
        lines.append(f"{role}: {text}")

    return "\n".join(lines) + f"\n\nUser: {messages[-1].content}"


def stream_reply(session: ChatSession) -> Generator[tuple[str, str], None, None]:
    """
    Yields (kind, value) tuples:
      ("token", text)   — streamed text chunk
      ("error", msg)    — on failure
    """
    # Pre-fetch all ORM data synchronously — Django ORM raises
    # SynchronousOnlyOperation if called inside asyncio.run().
    messages = list(session.messages.order_by("created_at"))
    if not messages or messages[-1].role != ChatMessage.USER:
        yield ("error", "No pending user message.")
        return

    prompt = _build_prompt(messages)
    system_prompt: str | None = (session.agent.system_prompt or None) if session.agent else None
    mcp_server_names: list[str] = list(session.agent.mcp_servers or []) if session.agent else []

    from tools import get_mcp_servers
    mcp_servers = get_mcp_servers(mcp_server_names)

    msg_queue: queue.Queue[tuple[str, object]] = queue.Queue()

    def _in_thread() -> None:
        async def _run() -> None:
            options = ClaudeAgentOptions(
                system_prompt=system_prompt,
                permission_mode="bypassPermissions",
                setting_sources=["user"],
                mcp_servers=mcp_servers,
            )
            try:
                async for msg in query(prompt=prompt, options=options):
                    msg_queue.put(("msg", msg))
            except Exception as exc:
                msg_queue.put(("error", str(exc)))
            msg_queue.put(("done", None))

        asyncio.run(_run())

    thread = threading.Thread(target=_in_thread, daemon=True)
    thread.start()

    while True:
        try:
            kind, payload = msg_queue.get(timeout=300)
        except queue.Empty:
            yield ("error", "Timeout — no response after 5 minutes.")
            break

        if kind == "done":
            break

        if kind == "error":
            yield ("error", str(payload))
            break

        msg = payload
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text:
                    yield ("token", block.text)
        elif isinstance(msg, ResultMessage):
            if msg.is_error and msg.errors:
                for err in msg.errors:
                    yield ("error", err)
