"""L1 unit floor — pure, deterministic functions in the scaffold that every crew inherits.

These guard the publish/rate path; a regression here ships a wrong-key/wrong-score deliverable to a
real owner. No LLM, no network: side effects are asserted by mocking ``_aimeat_call``, never by
reading printed ReAct text (which cannot tell a real call from a fabricated observation).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from crewaimeat import aimeat_crew as ac


# ---- memory-key slugging (a drift ships the deliverable to the WRONG key) ----
def test_memory_key_is_deterministic_and_slugged():
    task = {"id": "abc12345-def6-7890", "description": "Build a TicTacToe app!!!"}
    k1 = ac._memory_key("aimeat-app-builder", None, task)
    k2 = ac._memory_key("aimeat-app-builder", None, task)
    assert k1 == k2  # deterministic
    assert k1.startswith("crews.aimeat-app-builder.")  # default base (prefix=None)
    assert k1.endswith(".latest_output")
    assert "build-a-tictactoe-app" in k1  # lowercased, hyphen-slugged
    assert "abc12345" in k1  # short task-id token


def test_memory_key_honours_explicit_prefix():
    k = ac._memory_key("c", "news.frontpage", {"id": "manualrun", "title": "x"})
    assert k.startswith("news.frontpage.")


# ---- publish / verify directive parsing (a bad parse silently drops a write) ----
def test_parse_publish_directive_extracts_and_strips():
    key, tag, cleaned = ac._parse_publish_directive('do X <<AIMEAT_PUBLISH key="shared.k" tag="t1">> tail')
    assert key == "shared.k" and tag == "t1"
    assert "AIMEAT_PUBLISH" not in cleaned and "do X" in cleaned


def test_parse_publish_directive_absent():
    assert ac._parse_publish_directive("plain text") == (None, None, "plain text")


def test_parse_verify_directive():
    assert ac._parse_verify_directive("foo <<VERIFY>> bar")[0] == "on"
    assert ac._parse_verify_directive("foo <<NOVERIFY>> bar")[0] == "off"
    assert ac._parse_verify_directive("no directive here")[0] is None


# ---- directives / commands rendering (steerability of the live agent) ----
def test_format_directives_renders_purpose_and_rules():
    out = ac._format_directives({"purpose": "Be terse", "rules": [{"source": "owner", "description": "No emojis"}]})
    assert "Be terse" in out and "No emojis" in out


def test_format_directives_empty_is_blank():
    assert ac._format_directives({}) == ""
    assert ac._format_directives(None) == ""


def test_render_commands():
    assert ac._render_commands(None) == "_No commands declared._"
    table = ac._render_commands([{"name": "/go", "description": "start it"}])
    assert "/go" in table and "start it" in table and "|" in table


def test_humanize_name():
    assert ac._humanize_name("jingle-writer") == "Jingle Writer"
    assert ac._humanize_name("news_fetcher") == "News Fetcher"


def test_now_context_anchors_today():
    s = ac._now_context()
    assert isinstance(s, str) and "CURRENT TIME" in s


# ---- deterministic publish callback (must write the deliverable in code, not via the LLM) ----
def test_publish_cb_writes_primary_key_with_raw_text():
    with patch.object(ac, "_aimeat_call") as m:
        m.return_value = {"ok": True}
        cb = ac._make_publish_cb("agent-x", "crews.agent-x.foo.latest_output")
        out = MagicMock()
        out.raw = "THE DELIVERABLE"
        cb(out)
        writes = [c for c in m.call_args_list if c.args[1] == "aimeat_memory_write"]
        assert writes, "no aimeat_memory_write happened"
        payload = writes[0].args[2]
        assert payload["key"] == "crews.agent-x.foo.latest_output"
        assert payload["value"] == "THE DELIVERABLE"


def test_publish_cb_applies_clean_deliverable():
    with patch.object(ac, "_aimeat_call") as m:
        m.return_value = {"ok": True}
        cb = ac._make_publish_cb("agent-x", "k.latest_output", clean=lambda t: t.replace("DROP ", ""))
        out = MagicMock()
        out.raw = "DROP keep this"
        cb(out)
        payload = next(c for c in m.call_args_list if c.args[1] == "aimeat_memory_write").args[2]
        assert payload["value"] == "keep this"


# ---- task-nature classifier keyword fallback (must not depend on the LLM call succeeding) ----
def test_runtime_max_execution_time_reads_env(monkeypatch):
    monkeypatch.delenv("AIMEAT_AGENT_MAX_EXECUTION_TIME", raising=False)
    assert ac._runtime_max_execution_time() is None  # off by default
    monkeypatch.setenv("AIMEAT_AGENT_MAX_EXECUTION_TIME", "1800")
    assert ac._runtime_max_execution_time() == 1800
    monkeypatch.setenv("AIMEAT_AGENT_MAX_EXECUTION_TIME", "0")
    assert ac._runtime_max_execution_time() is None  # non-positive -> off
    monkeypatch.setenv("AIMEAT_AGENT_MAX_EXECUTION_TIME", "notanint")
    assert ac._runtime_max_execution_time() is None  # garbage -> off, never raises


def test_classify_nature_keyword_fallback_when_llm_errors():
    llm = MagicMock()
    llm.call.side_effect = RuntimeError("no network")
    creative = ac._classify_task_nature("Tell me a joke about cats", llm)
    assert creative["nature"] == "creative" and creative["verify"] == "off"
    fact = ac._classify_task_nature("Research the audited financials of Nokia Oyj", llm)
    assert fact["nature"] == "fact" and fact["verify"] == "factcheck"
