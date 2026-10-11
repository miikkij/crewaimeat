"""An agent's IDENTITY travels in the transport; its NAME spells its keys and its routes.

An agent whose crew is defined on the node runs with `spec.agent_name` = `name#owner@node`. The
daemon needs that form to pick the agent; a memory key or a URL built from it is wrong: the key is
one nothing reads, and in a URL the `#` starts a fragment, so the node sees a shorter path and
answers 404. Measured on production 2026-10-11 (agent `tarjoama-testi`): offer, README, evaluation
context and the task's live record all under `agents.tarjoama-testi#happydude500001@...`, and
`GET /v1/agents/tarjoama-testi/offers` empty.

Two lenses, because either alone lets the defect back in:
  * BEHAVIOUR — the publishers that failed, run with a full identity.
  * SHAPE — no source file may spell `agents.<expr>`, `crews.<expr>` or `/v1/agents/<expr>` itself.
    The spelling lives in `agent_manifest.agent_key` / `crew_key` / `agent_route`.

The same two lenses for what an agent has ON THIS MACHINE (second half of the file): a stored
credential is found by the agent's name (`crewaimeat.credentials`), a node call is signed by the
daemon and never by a bearer read off the disk, and a file name is built with `agent_local_name`
(a file filed under the name) or `agent_file_stem` (a file the runtime creates per identity).
"""

import ast
import json
import pathlib
import re

import pytest

from crewaimeat import aimeat_crew as ac
from crewaimeat import offers
from crewaimeat.agent_manifest import agent_key, agent_local_name, agent_route, crew_key

ROOT = pathlib.Path(__file__).resolve().parent.parent
GAII = "tarjoama-testi#happydude500001@aimeat-finland-001-genesis"
NAME = "tarjoama-testi"
META = {
    "id": "iskulause",
    "title": "Lyhyt iskulause tuotenimestä",
    "ask": "Anna tuotenimi. Palautan yhden iskulauseen; en tee muuta, only that.",
    "example": "Tuote: Kuura-kahvi",
    "tags": ["iskulause"],
    "cost": "cheap",
    "latency": "seconds",
    "repeatability": "idempotent",
    "verification": "ungated",
    "consequences": [],
}


# --- the helpers ------------------------------------------------------------------------------ #


@pytest.mark.parametrize("identity", [GAII, NAME, f"{NAME}#happydude500001"])
def test_helpers_spell_the_name_whatever_they_are_handed(identity):
    assert agent_local_name(identity) == NAME
    assert agent_key(identity) == "agents.tarjoama-testi"
    assert agent_key(identity, "offers") == "agents.tarjoama-testi.offers"
    assert agent_key(identity, "tasks", "t-1", "live") == "agents.tarjoama-testi.tasks.t-1.live"
    assert agent_key(identity, "statistics.custom.eval_context") == (
        "agents.tarjoama-testi.statistics.custom.eval_context"
    )
    assert crew_key(identity) == "crews.tarjoama-testi"
    assert crew_key(identity, "x", "latest_output") == "crews.tarjoama-testi.x.latest_output"
    assert agent_route(identity) == "/v1/agents/tarjoama-testi"
    assert agent_route(identity, "/offers") == "/v1/agents/tarjoama-testi/offers"


# --- behaviour: the publishers that filed under the wrong key ------------------------------------ #


class _Node:
    """Stands where the node is: records each REST call and each tool call, answers as told."""

    def __init__(self, rest_answer):
        self.rest_answer = rest_answer
        self.rest: list[tuple] = []
        self.calls: list[tuple] = []

    def install(self, monkeypatch):
        def rest(agent, method, path, body=None, **kw):
            self.rest.append((agent, method, path, body))
            return self.rest_answer

        def call(agent, tool, payload, **kw):
            self.calls.append((agent, tool, payload))
            return {"ok": True}

        monkeypatch.setattr(ac, "_aimeat_rest", rest)
        monkeypatch.setattr(ac, "_aimeat_call", call)
        monkeypatch.setattr(offers, "_aimeat_call", call)
        return self


_NOT_FOUND = {
    "ok": False,
    "http_status": 404,
    "error": {"code": "NOT_FOUND", "message": "Route not found: PUT /v1/agents/tarjoama-testi"},
}


def test_inline_offer_goes_to_the_route_by_name_with_the_identity_in_the_transport(monkeypatch):
    node = _Node({"count": 1}).install(monkeypatch)
    ok, detail = offers.publish_meta_offer(GAII, dict(META))
    assert ok, detail
    [(agent, method, path, body)] = node.rest
    assert agent == GAII, "the transport argument keeps the full identity: the daemon picks the agent by it"
    assert (method, path) == ("PUT", "/v1/agents/tarjoama-testi/offers")
    assert "#" not in path and "@" not in path
    assert "#" not in json.dumps(body), "the offer document names no identity either (deliverable location)"
    assert body["offers"][0]["deliverable"]["location"]["space"] == "crews.tarjoama-testi."
    assert node.calls == [], "a route that answered needs no second write"


def test_a_404_from_the_offer_route_is_a_failure_and_writes_nothing(monkeypatch):
    node = _Node(_NOT_FOUND).install(monkeypatch)
    ok, detail = offers.publish_meta_offer(GAII, dict(META))
    assert ok is False
    assert "404" in detail and "Route not found" in detail, "the caller is handed the node's own words"
    assert node.calls == [], "a 404 is not a node without offers: nothing is filed anywhere else"


def _one_offer_doc(monkeypatch):
    doc = {"offers": [offers.crew_offer(NAME, dict(META))]}
    monkeypatch.setattr(offers, "offers_doc_any", lambda agent, with_samples=False: doc)
    return doc


def test_declared_offers_go_to_the_route_by_name(monkeypatch):
    _one_offer_doc(monkeypatch)
    node = _Node({"count": 1}).install(monkeypatch)
    assert offers.publish_offers_any(GAII, with_samples=False) is True
    [(agent, method, path, _body)] = node.rest
    assert agent == GAII
    assert (method, path) == ("PUT", "/v1/agents/tarjoama-testi/offers")
    assert node.calls == []


def test_declared_offers_a_404_is_a_failure_and_writes_nothing(monkeypatch, capsys):
    _one_offer_doc(monkeypatch)
    node = _Node(_NOT_FOUND).install(monkeypatch)
    assert offers.publish_offers_any(GAII, with_samples=False) is False
    assert node.calls == []
    assert "Route not found" in capsys.readouterr().err


def test_declared_offers_an_unreachable_route_writes_the_key_the_node_reads(monkeypatch):
    """The one direct write that remains: the route could not be REACHED (no verdict at all)."""
    _one_offer_doc(monkeypatch)
    node = _Node(None).install(monkeypatch)
    assert offers.publish_offers_any(GAII, with_samples=False) is True
    [(agent, tool, payload)] = node.calls
    assert agent == GAII and tool == "aimeat_memory_write"
    assert payload["key"] == "agents.tarjoama-testi.offers"


def test_readme_is_filed_under_the_name(monkeypatch, tmp_path):
    node = _Node(None).install(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ac, "_expand_readme", lambda readme, llm, commands: readme)
    monkeypatch.setattr(ac, "get_llm", lambda **kw: None)
    ac._publish_readme(GAII, "# tarjoama-testi\n", None)
    writes = [(a, p["key"]) for a, tool, p in node.calls if tool == "aimeat_memory_write"]
    assert writes == [(GAII, "agents.tarjoama-testi.readme")]


def test_task_live_record_and_deliverable_are_filed_under_the_name():
    from crewaimeat.progress import ProgressReporter

    assert ProgressReporter(GAII)._live_key("044b7ea6") == "agents.tarjoama-testi.tasks.044b7ea6.live"
    key = ac._memory_key(GAII, None, {"id": "044b7ea6-1111", "title": "Iskulause"})
    assert key == "crews.tarjoama-testi.iskulause-044b7ea6.latest_output"


def test_the_llm_choice_key_is_by_name():
    from crewaimeat import llm_choice

    assert llm_choice.key_for(GAII) == "crews.llm.tarjoama-testi"


# --- the node's refusal of a misaddressed key: said once, never retried -------------------------- #

# The key holds "503" on purpose: a refusal quotes the key back, and a task id is hex.
_BAD_KEY = f"agents.{GAII}.tasks.0b503c1e-4616-4ae3-8d00-3d439f1e66a0.live"
_REFUSAL = {
    "ok": False,
    "error": {
        "code": "AGENT_KEY_NOT_A_NAME",
        "message": f"'{_BAD_KEY}' names an identity; write agents.tarjoama-testi.tasks.0b503c1e-….live",
    },
}


class _Refusing:
    """A serve daemon that hands back the node's 400, and counts how often it is asked."""

    def __init__(self):
        self.asked = 0

    def _answer(self):
        self.asked += 1

        class R:
            status_code = 400
            text = ""

            @staticmethod
            def json():
                return dict(_REFUSAL)

        return R()

    def post(self, url, **kw):
        return self._answer()

    def request(self, method, url, **kw):
        return self._answer()


@pytest.fixture
def refusing_daemon(monkeypatch):
    daemon = _Refusing()
    monkeypatch.setattr(ac, "_serve_api", lambda: ("http://127.0.0.1:1", daemon))
    monkeypatch.setattr(ac, "_identity_guard", lambda *a, **k: True)
    monkeypatch.setattr(ac.time, "sleep", lambda s: pytest.fail(f"backed off {s}s: the refusal was retried"))
    return daemon


def test_a_refused_memory_write_is_said_once_with_the_nodes_words_and_not_retried(refusing_daemon, capsys):
    res = ac._aimeat_call(GAII, "aimeat_memory_write", {"key": _BAD_KEY, "value": {}, "visibility": "owner"})
    assert res is None
    assert refusing_daemon.asked == 1
    err = capsys.readouterr().err
    assert err.count("AGENT_KEY_NOT_A_NAME") == 1 and "write agents.tarjoama-testi.tasks" in err


def test_a_refused_memory_patch_is_said_once_with_the_nodes_words_and_not_retried(refusing_daemon, capsys):
    res = ac._aimeat_rest(GAII, "PATCH", f"/v1/memory/{_BAD_KEY}", {"status": "working"})
    assert res is None
    assert refusing_daemon.asked == 1
    err = capsys.readouterr().err
    assert err.count("AGENT_KEY_NOT_A_NAME") == 1 and "write agents.tarjoama-testi.tasks" in err


# --- shape: nobody spells an agent's key or route by hand -------------------------------------- #

# What may be interpolated straight after one of these: only `agent_local_name(...)`.
_PREFIXES = ("agents.", "/v1/agents/", "crews.", "crews.llm.", "crews.runtime.", "crews.registry.")
# A template filled LATER (`KEY = "agents.{agent}.x"` ... `KEY.format(agent=...)`) is the same site.
_TEMPLATE = re.compile(r"(?:\bagents\.|\bcrews\.(?:llm\.|runtime\.|registry\.)?|/v1/agents/)(?:\{\w*\}|%s)")
# Every tracked directory that holds Python run against the node, plus the scripts at the root.
# tests/ is left out on purpose: a test may spell a wrong key to prove it is refused.
_SCANNED = ("src/crewaimeat", "crews", "scripts", "benchmarks")
_FIX = (
    "build it with crewaimeat.agent_manifest.agent_key / crew_key / agent_route (or registry_key / "
    "runtime_key / llm_choice.key_for): they strip a full identity to the name. The identity goes in "
    "the TRANSPORT argument, the first argument of _aimeat_call / _aimeat_rest."
)


def _strips(expr: ast.AST) -> bool:
    fn = expr.func if isinstance(expr, ast.Call) else None
    return isinstance(fn, ast.Name | ast.Attribute) and (getattr(fn, "id", None) or fn.attr) == "agent_local_name"


def _hand_spelled(tree: ast.AST):
    """(line, what) for every place a key or route is spelled from an expression that is not the name."""
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    in_fstring = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for lit, nxt in zip(node.values, node.values[1:], strict=False):
                if (
                    isinstance(lit, ast.Constant)
                    and isinstance(lit.value, str)
                    and lit.value.endswith(_PREFIXES)
                    and isinstance(nxt, ast.FormattedValue)
                    and not _strips(nxt.value)
                ):
                    yield node.lineno, f"...{lit.value[-14:]}{{{ast.unparse(nxt.value)}}}"
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = node.left
            if (
                isinstance(left, ast.Constant)
                and isinstance(left.value, str)
                and left.value.endswith(_PREFIXES)
                and not _strips(node.right)
            ):
                yield node.lineno, f"{left.value[-14:]!r} + {ast.unparse(node.right)}"
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and id(node) not in in_fstring
            and _TEMPLATE.search(node.value)
            and "\n" not in node.value  # prose (a prompt, a help text) is not a key
        ):
            yield node.lineno, f"template {node.value[:40]!r}"


def _offenders() -> list[str]:
    found = []
    files = sorted(ROOT.glob("*.py"))
    for rel in _SCANNED:
        files += sorted((ROOT / rel).rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, what in _hand_spelled(tree):
            found.append(f"{path.relative_to(ROOT).as_posix()}:{line}  {what}")
    return found


def test_no_file_spells_an_agent_key_or_route_from_a_raw_name():
    found = _offenders()
    assert not found, "an agent's key or route is spelled by hand:\n  " + "\n  ".join(found) + "\n" + _FIX


@pytest.mark.parametrize(
    "source",
    [
        'key = f"agents.{agent_name}.offers"',
        'path = f"/v1/agents/{spec.agent_name}/offers"',
        'key = f"crews.{agent}.latest_output"',
        'key = f"crews.runtime.{label}"',
        'key = "agents." + agent + ".readme"',
        'KEY = "agents.{agent}.library"',
        'PATH = "/v1/agents/%s/tasks"',
    ],
)
def test_the_rule_sees_each_shape_the_defect_took(source):
    assert list(_hand_spelled(ast.parse(source))), f"the rule missed: {source}"


@pytest.mark.parametrize(
    "source",
    [
        'key = f"crews.llm.{agent_local_name(agent_name)}"',
        'key = f"{agent_key(agent_name)}.offers"',
        'path = f"{agent_route(agent_name)}/offers"',
        'key = "agents.statistics"',
        'def f():\n    """GET /v1/agents/{name}/offers answers the list."""\n',
    ],
)
def test_the_rule_leaves_the_right_spellings_alone(source):
    assert not list(_hand_spelled(ast.parse(source))), f"the rule refused: {source}"


# =============================================================================================== #
# On this machine: the stored credential, the bearer nobody may send, and the file names
# =============================================================================================== #

OWNER = "happydude500001"
KEYED = f"keyed-agent#{OWNER}@aimeat-finland-001-genesis"  # enrolled with a key: no bearer on disk
OLD = f"old-agent#{OWNER}@aimeat-finland-001-genesis"  # still holds a v1 bearer


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A connector home holding one agent of each credential family, filed as the connector files them."""
    h = tmp_path / "aimeat"
    for rel, text in {
        f"keys/keyed-agent@{OWNER}.key": "KEY-MATERIAL",
        f"tokens/old-agent@{OWNER}.token": "BEARER",
        f"agents/{OWNER}/keyed-agent/config.yaml": "node_url: https://node.example\n",
        f"agents/{OWNER}/old-agent/config.yaml": "node_url: https://node.example\n",
    }.items():
        f = h / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    monkeypatch.setenv("AIMEAT_HOME", str(h))
    monkeypatch.delenv("AIMEAT_OWNER", raising=False)
    monkeypatch.chdir(tmp_path)
    return h


class _Response:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text, self.headers = payload, status, "", {}

    def json(self):
        return self._payload


@pytest.fixture
def daemon(monkeypatch):
    """The shared transport, recording. No test below patches a token lookup: this machine holds a KEY
    for the agent, and every call is expected to reach the transport anyway."""
    seen: list[tuple] = []
    answer = {"payload": {"ok": True, "data": {}}}

    def request(agent, method, path, **kw):
        seen.append((agent, method, path))
        return _Response(answer["payload"])

    monkeypatch.setattr(ac, "_aimeat_request", request)
    seen_answer = type("D", (), {"seen": seen, "answer": answer})
    return seen_answer


# --- the stored credential -------------------------------------------------------------------- #


def test_a_stored_credential_is_found_by_name_whatever_is_handed(home):
    from crewaimeat import credentials, generator_tool, public_url

    for handed in (OLD, "old-agent"):
        assert credentials.read_token(handed) == ("BEARER", "https://node.example")
        assert generator_tool._token(handed, None) == ("BEARER", "https://node.example")
        assert ac._aimeat_read_token(handed) == ("BEARER", "https://node.example")
    for handed in (KEYED, "keyed-agent"):
        assert credentials.read_token(handed) == ("", "https://node.example"), "a key is not a bearer"
        assert public_url._node_url(handed) == "https://node.example"


def test_the_owner_comes_from_the_identity_or_from_either_credential_family(home):
    from crewaimeat import generator_tool

    assert generator_tool._discover_owner("anything#someone@a-node") == "someone", "the identity says it"
    assert generator_tool._discover_owner("old-agent") == OWNER
    assert generator_tool._discover_owner("keyed-agent") == OWNER, "an agent with a key has an owner too"
    assert generator_tool._discover_owner("nobody-here") is None


def test_only_one_module_reads_the_packages_credential_reader():
    """A second import of it is a second place to forget the name."""
    readers = [
        path.relative_to(ROOT).as_posix()
        for rel in _SCANNED
        for path in sorted((ROOT / rel).rglob("*.py"))
        if re.search(r"import\s+_read_token\b", path.read_text(encoding="utf-8"))
    ]
    assert readers == ["src/crewaimeat/credentials.py"], readers


# --- the five modules, and the two that send a bearer the same way --------------------------------- #


def test_exchange_tools_reach_the_node_for_an_agent_with_no_bearer(home, daemon):
    from crewaimeat import exchange_tools

    tools = {t.name: t for t in exchange_tools.make_exchange_tools(KEYED)}
    out = tools["exchange_browse"].run(q="weather")
    assert "no token" not in out and not out.startswith("ERROR"), out
    [(agent, method, path)] = daemon.seen
    assert agent == KEYED and method == "GET" and path.startswith("/v1/exchange/offerings")


def test_schedule_tools_reach_the_node_for_an_agent_with_no_bearer(home, daemon):
    from crewaimeat import scheduler

    daemon.answer["payload"] = {
        "ok": True,
        "data": {"managed": [{"id": "s1", "kind": "agent_task", "cron": "0 6 * * *"}]},
    }
    tools = {t.name: t for t in scheduler.make_schedule_tools(KEYED)}
    out = tools["schedule_list"].run()
    assert "id=s1" in out, out
    assert daemon.seen == [(KEYED, "GET", "/v1/agents/keyed-agent/schedules")]


def test_a_refused_schedule_read_is_said_not_shown_as_no_schedules(home, monkeypatch):
    from crewaimeat import scheduler

    refusal = {"ok": False, "error": {"code": "SCOPE_DENIED", "message": 'Scope "schedule:read" required.'}}
    monkeypatch.setattr(ac, "_aimeat_request", lambda *a, **k: _Response(refusal, status=403))
    tools = {t.name: t for t in scheduler.make_schedule_tools(KEYED)}
    out = tools["schedule_list"].run()
    assert out.startswith("ERROR") and "SCOPE_DENIED" in out, out


def test_engagements_are_read_and_matched_by_name_for_an_agent_handed_in_by_identity(home, daemon):
    from crewaimeat import engagements

    daemon.answer["payload"] = {
        "ok": True,
        "data": {
            "engagements": [{"agentName": "keyed-agent", "contract": "c@1"}, {"agentName": "other", "contract": "x"}]
        },
    }
    got = engagements._agent_engagements(KEYED, "org-1", "ws-a")
    assert got == [{"agentName": "keyed-agent", "contract": "c@1"}], "None here means the gate fails open for good"
    assert daemon.seen == [(KEYED, "GET", "/v1/organisms/org-1/workspace/engagements")]


def test_reader_desk_publishes_a_readers_picture_without_a_stored_token(home, monkeypatch):
    from crewaimeat import public_url, reader_desk, seedream_gen, storage

    monkeypatch.setattr(storage, "fetch_bytes", lambda agent, key: (b"jpg", "image/jpeg"))
    monkeypatch.setattr(seedream_gen, "_upload_public", lambda *a, **k: True)
    monkeypatch.setattr(public_url, "person_link", lambda path, agent=None: "https://place.example" + path)
    monkeypatch.setattr(ac, "_aimeat_call", lambda *a, **k: pytest.fail("the identity was handed in; no roster read"))
    assert reader_desk.own_gaii(KEYED) == KEYED
    [link] = reader_desk.publish_tip_images(
        KEYED, [{"mime": "image/jpeg", "storage_key": "k", "name": "a.jpg"}], date="2026-10-11"
    )
    assert link == f"https://place.example/v1/pub/{KEYED}/sanomat/lukijoilta/2026-10-11/a.jpg"
    assert reader_desk.is_owner_human(KEYED, f"{OWNER}@aimeat-finland-001-genesis") is True


def test_a_rating_is_signed_by_the_daemon_not_sent_with_a_bearer_read_off_the_disk(home, daemon, monkeypatch):
    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: pytest.fail("a bearer was sent by hand"))
    ok, status, detail = ac._rate_task(KEYED, GAII, "t-1", {"stars": 4})
    assert (ok, status, detail) == (True, 200, "")
    assert daemon.seen == [(KEYED, "POST", "/v1/agents/tarjoama-testi/tasks/t-1/rate")]


def test_with_no_daemon_an_agent_with_a_key_is_told_so_and_no_empty_bearer_goes_out(home, monkeypatch, capsys):
    monkeypatch.setattr(ac, "_serve_api", lambda: None)
    monkeypatch.setattr(ac, "_identity_guard", lambda *a, **k: True)
    monkeypatch.setattr(
        ac.requests, "request", lambda *a, **k: pytest.fail("sent `Authorization: Bearer ` with nothing in it")
    )
    assert ac._aimeat_rest(KEYED, "GET", "/v1/agents/keyed-agent/skills", retries=1) is None
    assert "requires its serve daemon" in capsys.readouterr().err


# --- file names --------------------------------------------------------------------------------- #

STEM = "tarjoama-testi_happydude500001_aimeat-finland-001-genesis"


def test_file_stem_keeps_the_identity_and_is_a_legal_file_name():
    from crewaimeat import spawn_state
    from crewaimeat.agent_manifest import agent_file_stem, identity_owner

    assert agent_file_stem(GAII) == STEM
    assert agent_file_stem(NAME) == NAME, "a bare name is its own file name: nothing existing is renamed"
    assert identity_owner(GAII) == "happydude500001" and identity_owner(NAME) is None
    for handed in (GAII, NAME, "a/b\\c:d", ""):
        assert agent_file_stem(handed) == spawn_state._safe(handed), "the spawner's copy answers the same"


def test_what_the_runtime_keeps_per_agent_is_named_by_the_stem(home, monkeypatch, tmp_path):
    from crewaimeat import workflow

    assert ac._onboarding_marker(GAII).name == f"onboarding_{STEM}.attempt"
    assert workflow._explore_seq_path(GAII).name == f"{STEM}.seq"
    monkeypatch.setattr(ac, "_expand_readme", lambda readme, llm, commands: readme)
    monkeypatch.setattr(ac, "get_llm", lambda **kw: None)
    monkeypatch.setattr(ac, "_aimeat_call", lambda *a, **k: {"ok": True})
    ac._publish_readme(GAII, "# x\n", None)
    assert sorted(f.name for f in (tmp_path / "logs" / ".readme_cache").iterdir()) == [f"{STEM}.hash", f"{STEM}.md"]


def test_a_file_filed_under_the_name_is_found_when_handed_an_identity(home, tmp_path, monkeypatch):
    from crewaimeat import app_tools, brains, forge, forge_json, json_agent

    assert forge._fname(GAII) == forge._fname(NAME) == "tarjoama_testi_crew.py"
    assert forge_json._doc_base(GAII) == "tarjoama_testi"
    assert brains._stub_stem(GAII) == "tarjoama_testi_crew"
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    assert json_agent.staged_path(GAII) == tmp_path / "crew_defs" / "tarjoama_testi.json"
    assert forge._has_credential(KEYED, OWNER) is True
    assert app_tools._owner_of("keyed-agent") == OWNER


# --- shape: no file name is built from an agent's name as handed -------------------------------- #

_AGENTISH = {
    "agent",
    "agent_name",
    "ident",
    "identity",
    "who",
    "coordinator_name",
    "target_agent",
    "rater_agent",
    "ratee_agent",
}
# The spellings: by NAME (a file filed under it), by STEM (a file the runtime creates), and the older
# sanitisers that now strip to the name themselves (`_fname`, `_doc_base`, `_stub_stem`) or keep their
# own scheme on purpose (`_seg`: the memory store; `_known`: the appliance's own table).
_SPELLED = {"agent_local_name", "agent_file_stem", "_safe", "_fname", "_doc_base", "_stub_stem", "_seg", "_known"}
_PATH_CALLS = {"glob", "rglob", "join", "open", "Path", "mkdtemp", "with_name", "joinpath"}
_FILE_FIX = (
    "a file name is built from an agent's name as handed. Use agent_manifest.agent_local_name for a "
    "file filed under the NAME (a credential goes through crewaimeat.credentials), or "
    "agent_manifest.agent_file_stem for a file the runtime creates per agent (lock, cache, counter, log)."
)


def _is_agentish(expr: ast.AST) -> bool:
    return (isinstance(expr, ast.Name) and expr.id in _AGENTISH) or (
        isinstance(expr, ast.Attribute) and expr.attr in _AGENTISH
    )


def _raw_agent_names(operand: ast.AST):
    if isinstance(operand, ast.JoinedStr):
        for v in operand.values:
            if isinstance(v, ast.FormattedValue) and _is_agentish(v.value):
                yield ast.unparse(v.value)
    elif _is_agentish(operand):
        yield ast.unparse(operand)


def _raw_file_names(tree: ast.AST):
    """(line, what) for each path operand that carries an agent's name with no spelling around it."""
    for node in ast.walk(tree):
        operands: list[ast.AST] = []
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            operands = [node.left, node.right]
        elif isinstance(node, ast.Call):
            fn = node.func
            if (fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")) in _PATH_CALLS:
                operands = [*node.args, *(k.value for k in node.keywords if k.arg in ("prefix", "suffix"))]
        for operand in operands:
            for raw in _raw_agent_names(operand):
                yield node.lineno, f"{{{raw}}} in {ast.unparse(operand)[:60]}"


def test_no_file_name_is_built_from_an_agents_name_as_handed():
    files = sorted(ROOT.glob("*.py"))
    for rel in _SCANNED:
        files += sorted((ROOT / rel).rglob("*.py"))
    found = [
        f"{path.relative_to(ROOT).as_posix()}:{line}  {what}"
        for path in files
        for line, what in _raw_file_names(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert not found, "\n  ".join(["raw agent name in a file name:", *found]) + "\n" + _FILE_FIX


@pytest.mark.parametrize(
    "source",
    [
        'p = home / "tokens" / f"{agent_name}@{owner}.token"',
        'hits = tokens.glob(f"{agent}@*.token")',
        'lock = lock_dir / f"{spec.agent_name}.lock"',
        'p = cache_dir / f"{agent_name}.hash"',
        'cfg = agents_dir / owner / agent_name / "config.yaml"',
        'path = os.path.join(home, "keys", f"{agent}@{owner}.key")',
        'fh = open(f"logs/{agent}.log", "a")',
        'base = tempfile.mkdtemp(prefix=f"skills-{agent_name}-")',
    ],
)
def test_the_file_rule_sees_each_shape_the_defect_took(source):
    assert list(_raw_file_names(ast.parse(source))), f"the rule missed: {source}"


@pytest.mark.parametrize(
    "source",
    [
        'p = home / "tokens" / f"{agent_local_name(agent_name)}@{owner}.token"',
        'hits = tokens.glob(f"{glob.escape(agent_local_name(agent))}@*.token")',
        'lock = lock_dir / f"{agent_file_stem(spec.agent_name)}.lock"',
        "p = crews / _fname(agent_name)",
        "rate = done / total_agent_runs",
        'print(f"[{agent_name}] started")',
    ],
)
def test_the_file_rule_leaves_the_right_spellings_alone(source):
    assert not list(_raw_file_names(ast.parse(source))), f"the rule refused: {source}"
