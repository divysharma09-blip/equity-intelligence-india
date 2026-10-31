"""Sidebar AI assistant: answers market and app questions through Claude Code on the user's Mac.

Each question runs `claude -p` (the user's Claude plan, no API key) with:
* a system prompt describing the app (README) and the rules it must follow;
* the open company's data from the app (built by the UI layer and passed in as `context`);
* the recent conversation, so follow-ups work;
* tools: web search/fetch for market questions, and read-only file access to the app's own folder
  for questions about how the app works.

The answer is streamed back as ("text", chunk) and ("status", message) events.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from typing import Iterator, Literal, Optional

from eii.claude_cli import claude_env, find_claude
from eii.config import PROJECT_ROOT

DEFAULT_MODEL = os.getenv("EII_CHAT_MODEL", "claude-sonnet-5-5")
TIMEOUT_S = 6 * 60
HISTORY_TURNS = 6
TOOLS = ["WebSearch", "WebFetch", "Read", "Grep", "Glob"]

Event = tuple[Literal["text", "status", "error"], str]

SYSTEM = """You are the research assistant built into Equity Intelligence India, a stock-research app for
Indian listed companies. You answer two kinds of questions:

1. Markets: Indian and global equities, sectors, companies, macro (RBI, inflation, rates, GDP, rupee),
   indices, IPOs, regulations, investing concepts. Use web search for anything current (prices, news,
   results, policy) and cite sources as markdown links. Say the date of any market data you quote.
2. This app: how its pages, scores, data sources and methods work, and what the data for the open
   company shows. The app guide below covers the design; for detail you can Read/Grep/Glob the app's
   files in the working directory (code in eii/, tests in tests/, the four-investor master prompt in
   eii/four_investor/master_prompt.md). Never modify files.

Rules:
- Use the "Current app context" block for the open company; it is the app's verified data. If the user
  asks about the open company, start from it, and say when you add outside information.
- Never invent figures. If something is unavailable, say so. Distinguish facts, estimates and opinions.
- Use Indian conventions for Indian stocks: ₹, crore, lakh crore.
- You are not a financial adviser: give analysis and context, not personal buy/sell instructions, and
  point out key risks.
- Be concise: short paragraphs or bullets, a table when comparing. The answer shows in a narrow sidebar.

# App guide (README)

{readme}
"""


def available() -> tuple[bool, str]:
    path = find_claude()
    if path is None:
        return False, ("The AI assistant runs on Claude Code on your Mac. Start the app locally "
                       "(`streamlit run app.py`) to use it; Claude Code is not installed here.")
    return True, path


def _system_prompt() -> str:
    readme = PROJECT_ROOT / "README.md"
    return SYSTEM.format(readme=readme.read_text(encoding="utf-8") if readme.exists() else "(README missing)")


def build_prompt(question: str, history: list[dict], context: str) -> str:
    turns = history[-2 * HISTORY_TURNS:]
    convo = "\n\n".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in turns)
    return (f"# Current app context\n\n{context or 'No company is open in the app.'}\n\n"
            + (f"# Conversation so far\n\n{convo}\n\n" if convo else "")
            + f"# New question\n\n{question}")


def _status_for(block: dict) -> Optional[str]:
    kind, name = block.get("type"), block.get("name", "")
    if kind == "server_tool_use" or name in ("WebSearch", "web_search"):
        return "Searching the web…"
    if name in ("WebFetch", "web_fetch"):
        return "Reading a web page…"
    if name in ("Read", "Grep", "Glob"):
        return "Looking through the app's files…"
    return None


def parse_stream(lines: Iterator[str]) -> Iterator[Event]:
    """Turn `claude -p --output-format stream-json --include-partial-messages` lines into events."""
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "stream_event":
            ev = event.get("event", {})
            if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                yield "text", ev["delta"]["text"]
            elif ev.get("type") == "content_block_start":
                status = _status_for(ev.get("content_block", {}))
                if status:
                    yield "status", status
        elif kind == "result" and (event.get("is_error") or event.get("subtype") != "success"):
            msg = str(event.get("result") or event.get("subtype") or "unknown error")
            yield "error", (msg + " — if this is a usage limit, wait for your plan to reset."
                            if "limit" in msg.lower() else msg)


def ask(question: str, history: list[dict], context: str, model: str = DEFAULT_MODEL) -> Iterator[Event]:
    ok, path = available()
    if not ok:
        yield "error", path
        return
    cmd = [path, "-p", "--model", model, "--effort", "medium", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--system-prompt", _system_prompt(), "--no-session-persistence",
           "--strict-mcp-config", "--setting-sources", "", "--tools", *TOOLS, "--allowedTools", *TOOLS]
    stderr = tempfile.TemporaryFile(mode="w+")  # a file, not a pipe: an unread pipe could block the CLI
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                            text=True, env=claude_env(), cwd=PROJECT_ROOT)
    timer = threading.Timer(TIMEOUT_S, proc.kill)
    timer.start()
    got_text = False
    try:
        proc.stdin.write(build_prompt(question, history, context))
        proc.stdin.close()
        for kind, payload in parse_stream(proc.stdout):
            got_text |= kind == "text"
            yield kind, payload
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
    stderr.seek(0)
    err = stderr.read().strip()[-500:]
    stderr.close()
    if proc.returncode and not got_text:
        yield "error", f"Claude Code exited with an error ({proc.returncode}). {err}".strip()
