import json

from eii.chat import build_prompt, parse_stream
from eii.chat import assistant


def _ev(event):
    return json.dumps({"type": "stream_event", "event": event})


def test_parse_stream_yields_text_status_and_errors():
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        _ev({"type": "content_block_start", "content_block": {"type": "server_tool_use", "name": "web_search"}}),
        _ev({"type": "content_block_start", "content_block": {"type": "text", "text": ""}}),
        _ev({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "RBI held "}}),
        _ev({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "rates."}}),
        _ev({"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Read"}}),
        "not json",
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "RBI held rates."}),
    ]
    assert list(parse_stream(iter(lines))) == [("status", "Searching the web…"), ("text", "RBI held "),
                                              ("text", "rates."), ("status", "Looking through the app's files…")]
    err = json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "Usage limit reached"})
    kind, msg = list(parse_stream(iter([err])))[0]
    assert kind == "error" and "wait for your plan to reset" in msg


def test_prompt_carries_context_and_recent_history():
    history = [{"role": "user", "content": f"q{i}"} if i % 2 == 0 else {"role": "assistant", "content": f"a{i}"}
               for i in range(20)]
    p = build_prompt("What next?", history, "Open company: Alpha")
    assert p.startswith("# Current app context\n\nOpen company: Alpha")
    assert "User: q8" in p and "User: q6" not in p  # only the last 6 turns
    assert p.endswith("# New question\n\nWhat next?")
    assert "No company is open" in build_prompt("hi", [], "")


def test_unavailable_without_cli(monkeypatch):
    monkeypatch.setattr(assistant, "find_claude", lambda: None)
    ok, why = assistant.available()
    assert not ok and "on your Mac" in why
    assert list(assistant.ask("hi", [], ""))[0][0] == "error"
