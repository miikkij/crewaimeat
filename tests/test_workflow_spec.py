"""workflow_spec signal-evaluator floor — deterministic, no network, no LLM (judge faked)."""

import pytest

from crewaimeat.workflow_spec import (
    AGENT_SIGNALS,
    check_signal,
    check_workflow,
    resolve_step_signals,
    WORKFLOWS,
)


def lister_from(mem: dict):
    """A fake memory lister: fn(prefix) -> [{key,value}] over a {key: value} dict."""
    def _list(prefix: str) -> list[dict]:
        return [{"key": k, "value": v} for k, v in mem.items() if k.startswith(prefix)]
    return _list


VARS = {"date": "2026-06-11", "edition": "evening"}


def _full_edition(n_raw=12, n_art=12, quiz=True, editorial=True, space_weather=True,
                  frontpage_date="2026-06-11") -> dict:
    mem = {}
    for i in range(n_raw):
        mem[f"news.2026-06-11.evening.raw.cat{i}"] = f"raw text {i}"
    for i in range(n_art):
        mem[f"news.2026-06-11.evening.article.cat{i}"] = f"article body {i}"
    mem["news.2026-06-11.evening.article.talous"] = "Talousartikkeli sisältö."
    if space_weather:
        mem["news.2026-06-11.evening.article.avaruussaa"] = "Avaruussää tänään …"
    if quiz:
        mem["news.2026-06-11.evening.quiz"] = {"questions": [1, 2, 3, 4, 5]}
    if editorial:
        mem["news.2026-06-11.evening.editorial"] = "S.J. column …"
    if frontpage_date:
        # frontpage is a LIST of index items (matches the real structure)
        mem["newspaper.frontpage"] = [{"date": frontpage_date, "category": "talous", "title": "x"},
                                      {"date": "2026-06-10", "category": "old", "title": "y"}]
    return mem


# ── leaf checks ──────────────────────────────────────────────────────────────
def test_exists_nonempty_count():
    mem = {"news.2026-06-11.evening.raw.a": "x", "news.2026-06-11.evening.raw.b": ""}
    L = lister_from(mem)
    assert check_signal({"kind": "deterministic", "key": "news.{date}.{edition}.raw.a", "check": "exists"}, VARS, L)[0]
    assert check_signal({"kind": "deterministic", "key": "news.{date}.{edition}.raw.b", "check": "nonempty"}, VARS, L)[0] is False
    ok, obs = check_signal({"kind": "deterministic", "key_glob": "news.{date}.{edition}.raw.*",
                            "check": "count_nonempty", "min": 2}, VARS, L)
    assert ok is False and "1 nonempty" in obs  # only .a is nonempty


def test_json_array_match():
    mem = {"newspaper.frontpage": [{"date": "2026-06-11", "category": "talous"},
                                   {"date": "2026-06-10", "category": "old"}]}
    L = lister_from(mem)
    ok, obs = check_signal({"kind": "deterministic", "key": "newspaper.frontpage",
                            "check": "json_array_match", "where_field": "date",
                            "where_equals": "{date}", "min": 1}, VARS, L)
    assert ok and "1 item" in obs
    bad, _ = check_signal({"kind": "deterministic", "key": "newspaper.frontpage",
                           "check": "json_array_match", "where_field": "date",
                           "where_equals": "2026-06-99", "min": 1}, VARS, L)
    assert bad is False


def test_json_field_min_and_equals():
    mem = {"news.2026-06-11.evening.quiz": {"questions": [1, 2, 3]},
           "newspaper.frontpage": {"date": "2026-06-11"}}
    L = lister_from(mem)
    assert check_signal({"kind": "deterministic", "key": "news.{date}.{edition}.quiz",
                         "check": "json_field", "path": "questions", "min": 3}, VARS, L)[0]
    assert check_signal({"kind": "deterministic", "key": "news.{date}.{edition}.quiz",
                         "check": "json_field", "path": "questions", "min": 5}, VARS, L)[0] is False
    assert check_signal({"kind": "deterministic", "key": "newspaper.frontpage",
                         "check": "json_field", "path": "date", "equals": "{date}"}, VARS, L)[0]


# ── composition ──────────────────────────────────────────────────────────────
def test_all_any_compose():
    L = lister_from(_full_edition())
    sig_all = {"all": [
        {"kind": "deterministic", "key": "news.{date}.{edition}.editorial", "check": "nonempty"},
        {"kind": "deterministic", "key": "newspaper.frontpage", "check": "json_array_match",
         "where_field": "date", "where_equals": "{date}", "min": 1},
    ]}
    assert check_signal(sig_all, VARS, L)[0]
    sig_any = {"any": [
        {"kind": "deterministic", "key": "news.{date}.{edition}.missing", "check": "exists"},
        {"kind": "deterministic", "key": "news.{date}.{edition}.editorial", "check": "nonempty"},
    ]}
    assert check_signal(sig_any, VARS, L)[0]


def test_when_then_gate_skips_when_false():
    L = lister_from({})  # nothing present → the when-gate is false
    judged = {"called": False}

    def judge(ask, content):
        judged["called"] = True
        return True, "ok"

    sig = {"when": {"kind": "deterministic", "key": "news.{date}.{edition}.article.talous", "check": "nonempty"},
           "then": {"kind": "llm", "key": "news.{date}.{edition}.article.talous", "ask": "real?"}}
    ok, obs = check_signal(sig, VARS, L, judge)
    assert ok and "not applicable" in obs and judged["called"] is False  # llm never ran on empty


def test_llm_leaf_runs_behind_a_present_gate():
    L = lister_from(_full_edition())
    seen = {}

    def judge(ask, content):
        seen["content"] = content
        return False, "placeholder detected"

    sig = {"when": {"kind": "deterministic", "key": "news.{date}.{edition}.article.talous", "check": "nonempty"},
           "then": {"kind": "llm", "key": "news.{date}.{edition}.article.talous", "ask": "real?"}}
    ok, obs = check_signal(sig, VARS, L, judge)
    assert ok is False and "placeholder" in obs and "Talousartikkeli" in seen["content"]


def test_none_signal_always_passes():
    assert check_signal("none", VARS, lister_from({}))[0]


# ── offer inheritance ──────────────────────────────────────────────────────────
def test_resolve_inherits_offer_signals():
    fetch = next(s for s in WORKFLOWS["laimeat-sanomat-evening"]["steps"] if s["id"] == "fetch")
    req, succ = resolve_step_signals(fetch)
    assert req == "none"  # fetch has no input gate
    assert succ == AGENT_SIGNALS["fetch-edition-raw"]["success_signal"]  # inherited from offer
    # write-a / write-b each inherit their own offer's two-sided signals (no inline override now).
    write_a = next(s for s in WORKFLOWS["laimeat-sanomat-evening"]["steps"] if s["id"] == "write-a")
    w_req, w_succ = resolve_step_signals(write_a)
    assert w_req == AGENT_SIGNALS["evening-write-a"]["required_to_function"]
    assert w_succ == AGENT_SIGNALS["evening-write-a"]["success_signal"]
    assert w_succ["op"] == "count_nonempty"  # node grammar, not the old `check`


# ── full workflow test-run ────────────────────────────────────────────────────
def test_check_workflow_all_green():
    L = lister_from(_full_edition())
    res = check_workflow("laimeat-sanomat-evening", {"date": "2026-06-11", "edition": "evening"}, lister=L)
    assert {s["id"]: s["state"] for s in res["steps"]} == {
        "fetch": "GREEN", "write-a": "GREEN", "write-b": "GREEN",
        "space-weather": "GREEN", "features": "GREEN", "editorial": "GREEN"}


def test_check_workflow_red_at_editorial_output():
    mem = _full_edition(editorial=False, frontpage_date=None)  # articles present, editorial missing
    res = check_workflow("laimeat-sanomat-evening", {"date": "2026-06-11", "edition": "evening"},
                         lister=lister_from(mem))
    by = {s["id"]: s["state"] for s in res["steps"]}
    assert by["write-a"] == "GREEN" and by["write-b"] == "GREEN" and by["editorial"] == "output-RED"


def test_check_workflow_empty_is_input_red_downstream():
    res = check_workflow("laimeat-sanomat-evening", {"date": "2026-06-99", "edition": "evening"},
                         lister=lister_from({}))
    by = {s["id"]: s["state"] for s in res["steps"]}
    assert by["fetch"] == "output-RED"          # fetch has no input gate, its raw output is missing
    assert by["write-a"] == "input-RED"         # write's input (raw) is missing → blamed upstream
    assert by["editorial"] == "input-RED"
