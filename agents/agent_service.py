import asyncio
import html
import queue
import threading
from collections.abc import Generator

import markdown as md_lib
from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from .models import Agent

_MD = md_lib.Markdown(extensions=["fenced_code", "tables", "nl2br"])


def run_agent(agent: Agent, prompt: str) -> Generator[str, None, None]:
    """
    Yields HTML fragments suitable for SSE streaming.
    Each string may contain newlines — the SSE view must encode them correctly.
    """
    msg_queue: queue.Queue[tuple[str, object]] = queue.Queue()

    def _in_thread() -> None:
        async def _run() -> None:
            from tools import get_mcp_servers
            options = ClaudeAgentOptions(
                system_prompt=agent.system_prompt or None,
                cwd=agent.working_directory or None,
                permission_mode="bypassPermissions",
                setting_sources=["user"],
                mcp_servers=get_mcp_servers(agent.mcp_servers or []),
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
            yield _p("Timeout — agent did not respond within 5 minutes.", cls="output-error")
            break

        if kind == "done":
            yield _p("✓ Completed", cls="output-done")
            break

        if kind == "error":
            yield _p(f"Error: {html.escape(str(payload))}", cls="output-error")
            break

        msg = payload
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    _MD.reset()
                    rendered = _MD.convert(block.text)
                    yield f'<div class="output-text">{rendered}</div>'

                elif isinstance(block, ToolUseBlock):
                    preview = _input_preview(block.input)
                    yield (
                        f'<div class="output-tool">'
                        f'<span class="output-tool-icon">⚙</span> '
                        f'<strong>{html.escape(block.name)}</strong>'
                        f'<span class="output-tool-args">({html.escape(preview)})</span>'
                        f'</div>'
                    )

                elif isinstance(block, ToolResultBlock):
                    if isinstance(block.content, str) and block.content.strip():
                        lines = block.content.splitlines()[:8]
                        joined = "\n".join(html.escape(ln) for ln in lines if ln.strip())
                        if joined:
                            yield f'<pre class="output-result">{joined}</pre>'

        elif isinstance(msg, ResultMessage):
            if msg.is_error and msg.errors:
                for err in msg.errors:
                    yield _p(html.escape(err), cls="output-error")
            if msg.total_cost_usd is not None:
                yield _p(f"Cost: ${msg.total_cost_usd:.4f}", cls="output-cost")
            yield _p("✓ Completed", cls="output-done")
            break


def _p(content: str, cls: str = "") -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<p{attr}>{content}</p>"


def _input_preview(inp: dict[str, object]) -> str:
    parts = [f"{k}={repr(v)[:40]}" for k, v in list(inp.items())[:2]]
    return ", ".join(parts)
