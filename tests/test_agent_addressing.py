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


# --- shape: nobody spells an agent's key or route by hand -------------------------------------- #

# What may be interpolated straight after one of these: only `agent_local_name(...)`.
_PREFIXES = ("agents.", "/v1/agents/", "crews.", "crews.llm.", "crews.runtime.", "crews.registry.")
# A template filled LATER (`KEY = "agents.{agent}.x"` ... `KEY.format(agent=...)`) is the same site.
_TEMPLATE = re.compile(r"(?:\bagents\.|\bcrews\.(?:llm\.|runtime\.|registry\.)?|/v1/agents/)(?:\{\w*\}|%s)")
_SCANNED = ("src/crewaimeat", "crews", "scripts")
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
    for rel in _SCANNED:
        for path in sorted((ROOT / rel).rglob("*.py")):
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
