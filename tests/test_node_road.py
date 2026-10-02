"""The node road, and the hosted-place findings of 2026-10-02 that came with it.

The brief: aimeat-protocol doc-muqud1ah2zvl ("crews think through the node with the owner's own key").
A stand-in node answers like /v1/llm, as aimeat-crewai's own tests/test_crew_llm.py does it, because
the claim is about what goes on the wire. Also here: the provenance line a customer read, the date a
proposed agent guessed, the organism names it guessed, and the duplicate tools a crew-def produced.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import aimeat_crewai
import pytest

from crewaimeat import llm as llmmod
from crewaimeat import llm_choice, llm_road

# The real lookup, kept before the autouse fixture swaps it out for an older node.
_REAL_EFFECTIVE = llm_choice._effective

HAS_NODE_ROAD = hasattr(aimeat_crewai, "llm_for_choice")
needs_032 = pytest.mark.skipif(not HAS_NODE_ROAD, reason="the node road needs aimeat-crewai 0.32.0")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    llm_choice.forget()
    llm_choice._REFUSED_LOGGED.clear()
    llm_road.forget()
    monkeypatch.delenv("LLM_PROVIDERS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)  # no ./llm_providers.json unless a test writes one
    # Unless a test says otherwise, an older node: no effective-choice route, the stored keys decide.
    monkeypatch.setattr(llm_choice, "_effective", lambda name: llm_choice._NO_ANSWER)
    yield
    llm_choice.forget()
    llm_road.forget()


def _owner_chose(monkeypatch, agent: Any = None, default: Any = None) -> None:
    """What the node holds at crews.llm.<agent> and crews.llm.default."""

    def read(agent_name, key):
        return default if key == llm_choice.DEFAULT_KEY else agent

    monkeypatch.setattr("crewaimeat.memory_tools.read_owner_key", read)


class Reports:
    def __init__(self, answer=None):
        self.calls: list[dict] = []
        self.answer = {"ok": True} if answer is None else answer

    def __call__(self, agent_name, tool, payload, **kw):
        self.calls.append(payload)
        return self.answer


@pytest.fixture
def reports(monkeypatch):
    r = Reports()
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", r)
    return r


# ── a stand-in node that answers like /v1/llm ───────────────────────────────────────────────


class StandInNode:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        me = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                me.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(raw),
                    }
                )
                out = json.dumps(
                    {
                        "id": "c1",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "m",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "from the node"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def node(monkeypatch):
    n = StandInNode()
    # The OpenAI SDK's request headers ask platform.processor(), which runs `uname -p` on Linux: a
    # subprocess the offline guard refuses (it passed on Windows and failed on CI).
    monkeypatch.setattr("openai._base_client.get_platform", lambda: "Linux")
    monkeypatch.setenv("AIMEAT_NODE_URL", n.url)
    monkeypatch.setenv("AIMEAT_AGENT_TOKEN", "agent-token-1")
    yield n
    n.server.shutdown()


# ── 1. the node shape is accepted, at the agent's scope and the owner's default ────────────


@pytest.mark.parametrize("choice", [{"kind": "node"}, {"kind": "node", "role": "reasoning"}])
def test_the_node_shape_is_a_choice(choice):
    assert llm_choice._valid(choice) == choice


def test_a_node_shape_with_an_empty_role_is_not():
    assert llm_choice._valid({"kind": "node", "role": "  "}) is None


def test_the_owners_default_can_be_the_node(monkeypatch):
    _owner_chose(monkeypatch, default={"kind": "node"})
    assert llm_choice.node_choice("crm") == ({"kind": "node"}, "default")
    assert llm_choice.default_is_node_road("crm") is True


def _node_answers(monkeypatch, value, scope, why="", rest_calls=None):
    """The node's GET /v1/agents/{name}/crew/llm."""

    def rest(agent_name, method, path, **kw):
        if rest_calls is not None:
            rest_calls.append((agent_name, method, path))
        return {"value": value, "scope": scope, "why": why}

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_rest", rest)
    monkeypatch.setattr(llm_choice, "_effective", _REAL_EFFECTIVE)


def test_the_nodes_own_answer_decides_when_it_gives_one(monkeypatch):
    calls = []
    _node_answers(monkeypatch, {"kind": "node"}, "node", rest_calls=calls)
    _owner_chose(monkeypatch, agent={"kind": "profile", "profile": "stale"})  # the keys are not read
    assert llm_choice.node_choice("crm#owner1@n") == ({"kind": "node"}, "default")
    assert calls == [("crm#owner1@n", "GET", "/v1/agents/crm/crew/llm")], "the agent asks about itself"


def test_an_owners_node_default_does_not_apply_to_an_agent_without_ai_use(monkeypatch):
    """The keys say {kind:'node'}; the node knows the agent cannot use it, and answers the machine."""
    _node_answers(monkeypatch, None, None, why="The agent does not hold ai:use")
    _owner_chose(monkeypatch, default={"kind": "node"})
    assert llm_choice.node_choice("concierge") == (None, None)


def test_the_agents_own_choice_comes_back_as_its_own(monkeypatch):
    _node_answers(monkeypatch, {"kind": "profile", "profile": "coding"}, "agent")
    assert llm_choice.node_choice("crm") == ({"kind": "profile", "profile": "coding"}, "agent")


def test_an_older_node_without_the_route_falls_back_to_the_keys(monkeypatch):
    monkeypatch.setattr(
        "crewaimeat.aimeat_crew._aimeat_rest",
        lambda *a, **k: {"ok": False, "error": {"code": "NOT_FOUND"}, "http_status": 404},
    )
    monkeypatch.setattr(llm_choice, "_effective", _REAL_EFFECTIVE)
    _owner_chose(monkeypatch, default={"kind": "node"})
    assert llm_choice.node_choice("crm") == ({"kind": "node"}, "default")


# ── 2. resolved first, before the providers file and the environment ────────────────────────


@needs_032
@pytest.mark.loopback
def test_a_node_choice_sends_the_calls_to_the_node_with_the_agents_credential(monkeypatch, node, reports, tmp_path):
    (tmp_path / "llm_providers.json").write_text(
        json.dumps(
            {
                "default": "p",
                "profiles": {
                    "p": {"providers": [{"type": "openrouter", "api_key_env": "OPENROUTER_API_KEY", "models": ["x"]}]}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "the-places-key")
    _owner_chose(monkeypatch, default={"kind": "node", "role": "reasoning"})
    llm = llmmod.get_llm(agent_name="crm")
    out = llm.call([{"role": "user", "content": "hei"}])

    assert "from the node" in str(out)
    [req] = node.requests
    assert req["path"] == "/v1/llm/chat/completions"
    assert req["headers"]["authorization"] == "Bearer agent-token-1", "the agent's own credential"
    assert req["headers"].get("x-aimeat-ai-role") == "reasoning"
    assert "the-places-key" not in json.dumps(req), "the place's key goes nowhere"
    # The node picked the model; what crewai was configured with is a placeholder. The eval record names
    # the model the answer came from (the stand-in answers "m").
    from crewaimeat.aimeat_crew import _eval_ctx

    assert llmmod.resolved_model(llm) == "m"
    assert _eval_ctx({"model": llm.model, "llm": llm})["served_model"] == "m"


@needs_032
def test_a_local_pin_at_this_machine_stays_above_the_node(monkeypatch, node, reports):
    _owner_chose(monkeypatch, default={"kind": "node"})
    monkeypatch.setattr(
        llmmod,
        "agent_override",
        lambda a: {
            "kind": "model",
            "provider": {"type": "ollama", "base_url": "http://localhost:11434", "models": ["q"]},
        },
    )
    llm = llmmod._build_llm(True, 0.2, "crm")
    assert isinstance(llm, llmmod.MultiProviderLLM)
    assert node.requests == []


@needs_032
def test_a_node_choice_that_cannot_reach_the_node_fails_and_never_uses_the_places_key(monkeypatch, tmp_path):
    monkeypatch.delenv("AIMEAT_NODE_URL", raising=False)
    monkeypatch.delenv("AIMEAT_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "the-places-key")
    _owner_chose(monkeypatch, agent={"kind": "node"})
    with pytest.raises(llmmod.OwnerChoiceUnavailable, match="Not using this machine's key"):
        llmmod._build_llm(True, 0.2, "crm")


def test_without_the_node_road_in_the_package_a_node_choice_fails_loudly(monkeypatch):
    monkeypatch.delattr(aimeat_crewai, "llm_for_choice", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "the-places-key")
    _owner_chose(monkeypatch, agent={"kind": "node"})
    with pytest.raises(llmmod.OwnerChoiceUnavailable, match="aimeat-crewai 0.32.0"):
        llmmod._build_llm(True, 0.2, "crm")


# ── 3. every model choice is checked on read ────────────────────────────────────────────────


def _model_choice(**provider):
    return {"kind": "model", "label": "x", "provider": {"type": "openai", "models": [{"id": "m"}], **provider}}


@needs_032
@pytest.mark.parametrize(
    "provider",
    [
        {"api_key_env": "AIMEAT_ENCRYPTION_KEY"},
        {"api_key_env": "OPENROUTER_API_KEY", "base_url": "http://api.example.com/v1"},
        {"api_key_env": "OPENROUTER_API_KEY", "base_url": "https://10.0.0.5/v1"},
    ],
)
def test_an_unsafe_model_choice_is_no_choice_and_the_log_says_why_once(monkeypatch, capsys, provider):
    _owner_chose(monkeypatch, agent=_model_choice(**provider))
    assert llm_choice.node_choice("crm") == (None, None)
    llm_choice.forget()
    assert llm_choice.node_choice("crm") == (None, None)
    err = capsys.readouterr().err
    assert err.count("the owner's model choice is not used") == 1, err


@needs_032
def test_a_safe_model_choice_is_used(monkeypatch):
    choice = _model_choice(api_key_env="OPENROUTER_API_KEY")  # no address: nothing to resolve offline
    _owner_chose(monkeypatch, agent=choice)
    assert llm_choice.node_choice("crm") == (choice, "agent")


def test_a_runtime_without_the_guard_does_not_use_an_unchecked_model_choice(monkeypatch):
    monkeypatch.delattr(aimeat_crewai, "unsafe_choice_reason", raising=False)
    _owner_chose(monkeypatch, agent=_model_choice(api_key_env="OPENROUTER_API_KEY"))
    assert llm_choice.node_choice("crm") == (None, None)


# ── 4. no silent fall-back to the place's key for a choice the owner made ───────────────────


def test_an_owner_chosen_chain_whose_key_is_unset_fails_the_run_naming_it(monkeypatch, tmp_path):
    (tmp_path / "llm_providers.json").write_text(
        json.dumps(
            {
                "default": "free",
                "profiles": {
                    "free": {
                        "providers": [{"type": "openrouter", "api_key_env": "OPENROUTER_API_KEY", "models": ["x"]}]
                    },
                    "grok": {"providers": [{"type": "xai", "api_key_env": "XAI_API_KEY", "models": ["grok"]}]},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "the-places-key")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr(llm_choice, "node_choice", lambda a: ({"kind": "profile", "profile": "grok"}, "agent"))
    with pytest.raises(llmmod.OwnerChoiceUnavailable, match="XAI_API_KEY is not set"):
        llmmod._build_llm(True, 0.2, "crm")


def test_the_owners_default_profile_is_not_swapped_either(monkeypatch, tmp_path):
    (tmp_path / "llm_providers.json").write_text(
        json.dumps(
            {
                "default": "free",
                "profiles": {
                    "free": {
                        "providers": [{"type": "openrouter", "api_key_env": "OPENROUTER_API_KEY", "models": ["x"]}]
                    },
                    "grok": {"providers": [{"type": "xai", "api_key_env": "XAI_API_KEY", "models": ["grok"]}]},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "the-places-key")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr(llm_choice, "node_choice", lambda a: ({"kind": "profile", "profile": "grok"}, "default"))
    with pytest.raises(llmmod.OwnerChoiceUnavailable, match="node-default:grok"):
        llmmod._build_llm(True, 0.2, "unopinionated-agent")


def test_a_chain_nobody_chose_still_falls_back_as_before(monkeypatch, tmp_path, capsys):
    (tmp_path / "llm_providers.json").write_text(
        json.dumps(
            {
                "default": "grok",
                "profiles": {"grok": {"providers": [{"type": "xai", "api_key_env": "XAI_API_KEY", "models": ["g"]}]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(llm_choice, "node_choice", lambda a: (None, None))
    llmmod._build_llm(True, 0.2, "crm")
    assert "using env config" in capsys.readouterr().err


# ── 5. the road is reported, as the agent itself, once and on every change ─────────────────


def test_the_road_is_reported_once_and_again_when_it_changes(monkeypatch, reports):
    monkeypatch.setattr(llmmod, "_build_llm", lambda *a: object())
    monkeypatch.setattr(llmmod, "install_directives", lambda llm, a, o: llm)
    road = {"choice": None}
    monkeypatch.setattr(llmmod, "_node_road_choice", lambda a: road["choice"])

    llmmod.get_llm(agent_name="crm#owner1@node")
    llmmod.get_llm(agent_name="crm#owner1@node")
    road["choice"] = {"kind": "node"}
    llmmod.get_llm(agent_name="crm#owner1@node")

    assert [r["llm"] for r in reports.calls] == ["machine", "node"]
    first = reports.calls[0]
    assert first["target_agent_name"] == "crm", "about itself, by its own name"
    assert first["kind"] == "python" and first["runtime"].startswith("crewaimeat ")


def test_a_crew_def_reports_its_kind_and_revision(reports):
    llm_road.note_definition("crm", 4)
    llm_road.report("crm", "node")
    assert reports.calls[0]["kind"] == "crew-def" and reports.calls[0]["definition_revision"] == 4


def test_a_refused_report_is_said_and_not_repeated(monkeypatch, capsys):
    r = Reports(answer={"ok": False, "error": {"code": "SCOPE_DENIED", "message": "needs agent:write"}})
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", r)
    assert llm_road.report("crm", "node") is False
    assert llm_road.report("crm", "node") is False
    assert len(r.calls) == 1
    assert "SCOPE_DENIED" in capsys.readouterr().err


# ── 6. ai:use is asked for on the node road, agent:write no longer ──────────────────────────


def test_what_every_run_writes_with_no_longer_includes_agent_write():
    from crewaimeat.agent_scopes import RUNTIME_WRITE_SCOPES, runtime_scopes

    assert RUNTIME_WRITE_SCOPES == ("memory:write",)
    assert runtime_scopes(node_road=False) == ["memory:write"]
    assert runtime_scopes(node_road=True) == ["memory:write", "ai:use"]


def test_the_menu_asks_for_ai_use_when_the_owners_default_is_the_node(monkeypatch):
    import crewaimeat.crew_invoke as ci

    monkeypatch.setattr("crewaimeat.llm_choice.default_is_node_road", lambda a: True)
    monkeypatch.setattr("crewaimeat.crew_invoke._decide_rule_rows", lambda a: [])
    ok, menu = ci.handle("crew.menu", {}, agent_name="concierge")
    assert ok and menu["required_scopes"] == ["memory:write", "ai:use"]


def test_a_proposal_asks_for_ai_use_on_the_node_road():
    from crewaimeat.concierge_propose import compute_scopes

    scopes, _ = compute_scopes(["workspace_write"], ["*"], node_road=True)
    assert "ai:use" in scopes and "agent:write" not in scopes
    scopes, _ = compute_scopes(["workspace_write"], ["*"], node_road=False)
    assert "ai:use" not in scopes


# ── the provenance line a customer read ─────────────────────────────────────────────────────

MEASURED = (
    "**Deals to act on today**\n\n1. Koe Oy -- overdue\n2. Rantanen Ky -- today\n\n---\n"
    '*ai_provenance*: {"level":"ai-generated"}\n'
)


def test_the_measured_provenance_line_is_taken_out():
    from crewaimeat.verify_report import split_provenance

    body, taken = split_provenance(MEASURED)
    assert "provenance" not in body and body.rstrip().endswith("Rantanen Ky -- today")
    assert taken == ['*ai_provenance*: {"level":"ai-generated"}']


def test_a_multiline_declaration_goes_whole_and_a_sentence_about_provenance_stays():
    from crewaimeat.verify_report import split_provenance

    text = 'The answer.\n\n"ai_provenance": {\n  "level": "ai-generated",\n  "model": "x"\n}\nTail line.\n'
    body, taken = split_provenance(text)
    assert body == "The answer.\n\nTail line.\n" and len(taken) == 1
    keep = "We record the provenance of every document."
    assert split_provenance(keep) == (keep, [])


def test_every_deliverable_is_cleaned_not_only_a_verified_one():
    from crewaimeat.aimeat_crew import _for_the_reader

    assert "provenance" not in _for_the_reader(None, verified=False)(MEASURED)


def test_a_dm_reply_is_cleaned_too(monkeypatch):
    from crewaimeat import dm

    sent = {}
    monkeypatch.setattr(dm, "dm_reply", lambda agent, to, text, **kw: sent.setdefault("text", text) or True)
    monkeypatch.setattr(dm, "_inbound_fields", lambda e: ("m1", "c1", "someone#x@n", "hei", ""))
    dm.handle_dm_event("concierge", {"id": "m1"}, lambda e: MEASURED, seen=set())
    assert "provenance" not in sent["text"]


def test_the_directives_say_where_provenance_belongs_only_when_a_rule_asks_for_it():
    from crewaimeat.directives import format_directives

    rule = {"source": "system", "description": "Say how content was made: declare `ai_provenance`."}
    assert "Never write ai_provenance" in format_directives({"rules": [rule]})
    other = {"source": "owner", "description": "Answer in Finnish."}
    assert "ai_provenance" not in format_directives({"rules": [other]})


# ── the date reaches a crew-def on every run ────────────────────────────────────────────────

DOC = {
    "agent_name": "morning-deals",
    "agents": [{"role": "Clerk", "goal": "g", "backstory": "b", "tools": []}],
    "tasks": [{"id": "run", "agent": "Clerk", "description": "Brief me. {{ctx.prompt}}", "expected_output": "x"}],
}


def _ctx(**kw):
    return SimpleNamespace(
        prompt="the request", today="TODAY IS 2026-10-02 (Friday)", directives="", llm=None, task={"id": "t"}, **kw
    )


def test_the_date_is_in_every_task_of_a_crew_def():
    from crewaimeat.crew_def import build_domain_from_json

    _agents, [task] = build_domain_from_json(DOC, _ctx())
    assert task.description.startswith("TODAY IS 2026-10-02 (Friday)")


def test_a_definition_that_places_the_date_itself_gets_it_once():
    from crewaimeat.crew_def import build_domain_from_json

    doc = json.loads(json.dumps(DOC))
    doc["tasks"][0]["description"] = "Brief me for {{ctx.today}}. {{ctx.prompt}}"
    _agents, [task] = build_domain_from_json(doc, _ctx())
    assert task.description.count("2026-10-02") == 1


# ── a crew-def naming workspace and workspace_write gets each tool once ────────────────────


def test_duplicate_tool_names_are_dropped_and_said(monkeypatch, capsys):
    from crewaimeat.crew_def import build_domain_from_json

    doc = json.loads(json.dumps(DOC))
    doc["agents"][0]["tools"] = ["workspace", "workspace_write", "decline"]
    [agent], _tasks = build_domain_from_json(doc, _ctx())
    names = [t.name for t in agent.tools]
    assert names == [
        "list_workspaces",
        "read_workspace",
        "write_workspace_record",
        "append_workspace_rows",
        "decline_request",
    ]
    err = capsys.readouterr().err
    assert "dropped duplicate tool(s) list_workspaces, read_workspace" in err


def test_the_first_of_two_same_named_tools_is_the_one_kept():
    from crewaimeat.crew_def import _unique_tools

    a, b = SimpleNamespace(name="x", n=1), SimpleNamespace(name="x", n=2)
    assert _unique_tools([a, b], "crm/Clerk") == [a]


# ── the agent names the workspace; code finds it ────────────────────────────────────────────


class Spaces:
    def __init__(self, workspaces):
        self.workspaces = workspaces  # [(org_id, org_name, ws_id, ws_name)]
        self.reads: list[dict] = []

    def __call__(self, agent_name, tool, payload, **kw):
        if tool == "aimeat_organism_list":
            orgs = {(o, n) for o, n, _w, _wn in self.workspaces}
            return {"organisms": [{"id": o, "name": n} for o, n in sorted(orgs)]}
        if tool == "aimeat_workspace_list":
            return {
                "workspaces": [{"id": w, "name": wn} for o, _n, w, wn in self.workspaces if o == payload["organism_id"]]
            }
        if tool == "aimeat_workspace_read":
            self.reads.append(payload)
            return {"spaces": ["deal"]}
        return None


@pytest.fixture
def spaces(monkeypatch):
    from crewaimeat import workspace_tools

    def install(*rows):
        s = Spaces(list(rows))
        monkeypatch.setattr(workspace_tools, "_aimeat_call", s)
        return {t.name: t for t in workspace_tools.make_workspace_tools("crm")}, s

    return install


def test_a_workspace_is_read_by_its_name(spaces):
    tools, s = spaces(("o1", "Koe Oy", "ws-1", "CADENCE"))
    out = tools["read_workspace"].run(workspace="cadence")
    assert "deal" in out and s.reads == [{"organism_id": "o1", "ws": "ws-1"}]


def test_a_guessed_name_is_answered_with_what_exists(spaces):
    tools, s = spaces(("o1", "Koe Oy", "ws-1", "CADENCE"))
    out = tools["read_workspace"].run(workspace="default")
    assert "no workspace 'default'" in out and "CADENCE" in out and "Koe Oy" in out
    assert s.reads == [], "nothing is sent for a guess"


def test_two_of_one_name_are_told_apart_by_the_organism(spaces):
    tools, s = spaces(("o1", "Koe Oy", "ws-1", "CRM"), ("o2", "Toinen Oy", "ws-2", "CRM"))
    out = tools["read_workspace"].run(workspace="CRM")
    assert "More than one workspace is called 'CRM'" in out and s.reads == []
    tools["read_workspace"].run(workspace="CRM", organism="Toinen Oy")
    assert s.reads == [{"organism_id": "o2", "ws": "ws-2"}]


def test_a_proposal_without_a_workspace_tells_the_agent_to_list_first():
    from crewaimeat.concierge_propose import build_crew_def

    d = build_crew_def(
        name="crm-clerk",
        display_name="CRM",
        purpose="Keeps the CRM.",
        instructions="Do it.",
        tools=["workspace"],
        workspace=None,
        delivers="",
    )
    assert "Call list_workspaces first" in d["tasks"][0]["description"]


@pytest.mark.loopback
def test_a_provider_chain_records_the_model_that_answered(node):
    """The same capture for every crew on a provider chain: crewai 1.15 reads the raw response, and
    the wrap that only covered `create` recorded the configured id for every call (found 2026-10-02)."""
    ep = {
        "label": "stand-in:x",
        "provider": "openai",
        "model": "openai/configured-id",
        "base_url": node.url + "/v1",
        "api_key": "k",
        "context": 8000,
        "additional_params": {},
    }
    llm = llmmod.MultiProviderLLM([ep], 0.2)
    llm.call([{"role": "user", "content": "hei"}])
    assert llmmod.resolved_model(llm) == "m"
    assert llmmod.resolved_provider() == "openai"


@needs_032
def test_a_local_profile_pin_stays_above_the_node_too(monkeypatch, node, reports, tmp_path):
    (tmp_path / "llm_providers.json").write_text(
        json.dumps({"default": "p", "profiles": {"p": {"providers": [{"type": "ollama", "models": ["q"]}]}}}),
        encoding="utf-8",
    )
    _owner_chose(monkeypatch, default={"kind": "node"})
    monkeypatch.setattr(llmmod, "agent_override", lambda a: {"kind": "profile", "profile": "p"})
    llm = llmmod.get_llm(agent_name="crm")
    assert node.requests == [] and "node" not in [r["llm"] for r in reports.calls]
    assert llm is not None
