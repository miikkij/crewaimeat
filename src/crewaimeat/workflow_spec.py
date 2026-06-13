"""Agent Workflows — descriptor + signal evaluator (crew-side reference impl).

The executable form of docs/internal/2026-06-13-agent-workflows-node-spec.md. A workflow is an
ordered set of steps; each step declares two signal trees — `required_to_function` (the INPUT it
needs, consumer-owned, checked at start) and `success_signal` (its OUTPUT, producer-owned, checked
at end). Signals are inherited from the agent's Offer (AGENT_SIGNALS below — the same data
offers.py publishes) and may be overridden per step.

A signal is a tree evaluated against owner memory with `{var}` templated from the run params:
  leaf  {kind: deterministic, key|key_glob, check, ...}   — no LLM, the happy path
  leaf  {kind: llm, key|key_glob, ask}                     — judge returns ok+reason (node OpenRouter
                                                              in prod; local get_llm here)
  comp  {all:[...]} | {any:[...]} | {when:<sig>, then:<sig>}
  the literal "none"                                        — no gate, always OK

Deterministic checks: exists · nonempty · count_nonempty(min) · json_valid ·
json_field(path, min|equals|nonempty). Pure functions over a fake memory map are unit-tested.
"""

from __future__ import annotations

import fnmatch
import json
import re
from typing import Any, Callable

from crewaimeat.aimeat_crew import _aimeat_call


# ── memory access (injectable for tests) ─────────────────────────────────────
def _default_reader(agent: str) -> Callable[[str], list[dict]]:
    """Return a fn(prefix) -> [{key, value}] listing owner-scope memory under a prefix."""
    def _list(prefix: str) -> list[dict]:
        r = _aimeat_call(agent, "aimeat_memory_list",
                         {"owner_scope": True, "prefix": prefix, "limit": 500}) or {}
        items = r.get("items") or []
        out = []
        for it in items:
            v = it.get("value")
            if v is None:
                v = (_aimeat_call(agent, "aimeat_memory_read", {"key": it.get("key")}) or {}).get("value")
            out.append({"key": it.get("key"), "value": v})
        return out
    return _list


def _templ(s: Any, vars: dict) -> Any:
    if not isinstance(s, str):
        return s
    for k, v in vars.items():
        s = s.replace("{" + k + "}", str(v))
    return s


def _nonempty(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return True


def _as_obj(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return None


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _entries(lister, key_glob: str) -> list[dict]:
    """Entries matching a glob (prefix list + fnmatch), or a single key."""
    if "*" in key_glob:
        prefix = key_glob.split("*", 1)[0]
        return [e for e in lister(prefix) if fnmatch.fnmatch(e.get("key", ""), key_glob)]
    return [e for e in lister(key_glob) if e.get("key") == key_glob]


def check_signal(node: Any, vars: dict, lister, llm_judge=None) -> tuple[bool, str]:
    """Recursively evaluate a signal tree. Returns (ok, observed-description)."""
    if node in (None, "none"):
        return True, "no gate"
    if not isinstance(node, dict):
        return False, f"malformed signal: {node!r}"

    if "all" in node:
        results = [check_signal(c, vars, lister, llm_judge) for c in node["all"]]
        ok = all(r[0] for r in results)
        fails = [r[1] for r in results if not r[0]]
        return ok, ("all pass" if ok else "; ".join(fails))
    if "any" in node:
        results = [check_signal(c, vars, lister, llm_judge) for c in node["any"]]
        ok = any(r[0] for r in results)
        return ok, ("≥1 passes" if ok else "none of the alternatives passed")
    if "when" in node:
        w_ok, _ = check_signal(node["when"], vars, lister, llm_judge)
        if not w_ok:
            return True, "when-gate not applicable (skipped)"
        return check_signal(node["then"], vars, lister, llm_judge)

    kind = node.get("kind", "deterministic")
    if kind == "llm":
        key_glob = _templ(node.get("key") or node.get("key_glob"), vars)
        ents = _entries(lister, key_glob)
        content = "\n\n".join(str(e.get("value") or "")[:1500] for e in ents if _nonempty(e.get("value")))
        if not content:
            return False, f"llm signal: no content at {key_glob}"
        if llm_judge is None:
            return True, "llm signal skipped (no judge wired)"
        ok, reason = llm_judge(node.get("ask", ""), content)
        return ok, f"llm: {reason}"

    # deterministic leaf
    key_glob = _templ(node.get("key") or node.get("key_glob") or "", vars)
    check = node.get("op") or node.get("check") or "exists"  # node grammar uses `op`; `check` kept as alias
    ents = _entries(lister, key_glob)
    if check == "count_nonempty":
        n = sum(1 for e in ents if _nonempty(e.get("value")))
        need = int(node.get("min", 1))
        return n >= need, f"{n} nonempty at {key_glob} (need {need})"
    val = ents[0].get("value") if ents else None
    if check == "exists":
        return bool(ents), f"{'exists' if ents else 'missing'}: {key_glob}"
    if check == "nonempty":
        return _nonempty(val), f"{'nonempty' if _nonempty(val) else 'empty/missing'}: {key_glob}"
    if check == "json_valid":
        return _as_obj(val) is not None, f"{'valid json' if _as_obj(val) is not None else 'not json'}: {key_glob}"
    if check == "json_field":
        obj = _as_obj(val)
        field = _dig(obj, node.get("path", "")) if obj is not None else None
        if "equals" in node:
            want = _templ(node["equals"], vars)
            return field == want, f"{key_glob}.{node.get('path')} = {field!r} (want {want!r})"
        if "min" in node:
            n = len(field) if isinstance(field, (list, str, dict)) else (field or 0)
            return n >= int(node["min"]), f"{key_glob}.{node.get('path')} size {n} (need {node['min']})"
        return _nonempty(field), f"{key_glob}.{node.get('path')} {'present' if _nonempty(field) else 'missing'}"
    if check == "json_array_match":
        # val is a JSON array; count items where item[where_field] == templated where_equals.
        arr = _as_obj(val)
        field, want = node.get("where_field"), _templ(node.get("where_equals"), vars)
        n = sum(1 for it in (arr or []) if isinstance(it, dict) and it.get(field) == want) if isinstance(arr, list) else 0
        need = int(node.get("min", 1))
        return n >= need, f"{key_glob}: {n} item(s) with {field}={want!r} (need {need})"
    return False, f"unknown check {check!r}"


# ── the agents' offered signals (the source offers.py publishes; workflow inherits) ──
# Node grammar (handbook "Agent Workflows"): a deterministic leaf is
#   {kind:"deterministic", key|key_glob, op: exists|nonempty|count_nonempty(min)|json_valid|
#    json_schema(schema)|json_field(path, min|equals|nonempty)}.  `op` (not `check`); no
#   json_array_match — so the editorial output check is just its own key being nonempty.
# Each entry ALSO carries deliverable_location.key — where the agent WRITES — which the node
# assembles into the workflow blueprint. offers.py emits all three onto the published offer.
_RAW = "news.{date}.{edition}.raw.*"
_ART = "news.{date}.{edition}.article.*"
_RAW_MIN = 12     # ~20 categories fetched; loud floor
_ART_MIN = 12     # the day's article set across both desks
_DOWN_MIN = 3     # features/editorial just need a handful to work on

AGENT_SIGNALS: dict[str, dict] = {
    # offer id -> {required_to_function, success_signal, deliverable_location}
    "fetch-edition-raw": {
        "required_to_function": "none",   # reads live feeds, not memory — no input gate
        "success_signal": {"kind": "deterministic", "key_glob": _RAW, "op": "count_nonempty", "min": _RAW_MIN},
        "deliverable_location": {"key": _RAW},
    },
    # Desk A + Desk B both write into the shared news.<date>.<edition>.article.* namespace
    # (write_pipeline.DESK_A / DESK_B). Two separate steps (one agent each); each step's output
    # check is the article set filling up. NB the shared namespace means one desk's count can
    # include the other's — the gate reliably catches the whole-pipeline-dry failure (the 06-12
    # class) even if it can't perfectly attribute a single silent desk.
    "evening-write-a": {
        "required_to_function": {"kind": "deterministic", "key_glob": _RAW, "op": "count_nonempty", "min": _RAW_MIN},
        "success_signal": {"kind": "deterministic", "key_glob": _ART, "op": "count_nonempty", "min": _ART_MIN},
        "deliverable_location": {"key": _ART},
    },
    "evening-write-b": {
        "required_to_function": {"kind": "deterministic", "key_glob": _RAW, "op": "count_nonempty", "min": _RAW_MIN},
        "success_signal": {"kind": "deterministic", "key_glob": _ART, "op": "count_nonempty", "min": _ART_MIN},
        "deliverable_location": {"key": _ART},
    },
    "evening-features": {
        "required_to_function": {"kind": "deterministic", "key_glob": _ART, "op": "count_nonempty", "min": _DOWN_MIN},
        "success_signal": {"kind": "deterministic", "key": "news.{date}.{edition}.quiz",
                           "op": "json_field", "path": "questions", "min": 3},
        "deliverable_location": {"key": "news.{date}.{edition}.quiz"},
    },
    "evening-editorial": {
        "required_to_function": {"kind": "deterministic", "key_glob": _ART, "op": "count_nonempty", "min": _DOWN_MIN},
        "success_signal": {"kind": "deterministic", "key": "news.{date}.{edition}.editorial", "op": "nonempty"},
        "deliverable_location": {"key": "news.{date}.{edition}.editorial"},
    },
    "space-weather": {
        "required_to_function": "none",   # fetches NOAA/NASA itself; independent of the fetch step
        "success_signal": {"kind": "deterministic", "key": "news.{date}.{edition}.article.avaruussaa", "op": "nonempty"},
        "deliverable_location": {"key": "news.{date}.{edition}.article.avaruussaa"},
    },
}


# ── workflow definitions ─────────────────────────────────────────────────────
WORKFLOWS: dict[str, dict] = {
    "laimeat-sanomat-evening": {
        "id": "laimeat-sanomat-evening",
        "title": {"fi_FI": "(L)AIMEAT Sanomat — iltapainos", "en_US": "(L)AIMEAT Sanomat — evening"},
        "description": {"fi_FI": "Iltapainoksen tuotantoketju: hae raaka → kirjoita → erikoisosiot+visa → editoriaali+etusivu.",
                        "en_US": "Evening edition pipeline: fetch raw → write → features+quiz → editorial+frontpage."},
        "schedule": {"cron": "0 17 * * *", "timezone": "Europe/Helsinki"},
        "vars": [
            {"name": "date", "type": "date", "default": "<run-date>", "example": "2026-06-11",
             "description": {"fi_FI": "Painoksen päivä (YYYY-MM-DD)", "en_US": "Edition date (YYYY-MM-DD)"}},
            {"name": "edition", "type": "string", "default": "evening",
             "description": {"fi_FI": "Painos (evening/morning)", "en_US": "Edition (evening/morning)"}},
        ],
        "steps": [
            {"id": "fetch", "agent": "news-fetcher", "offer": "fetch-edition-raw",
             "description": {"fi_FI": "Hae päivän raakauutismateriaali per kategoria.",
                             "en_US": "Fetch the day's raw news per category."},
             "stage": ("crewaimeat.fetch_pipeline", "build_edition_raw")},
            # Desk A + Desk B run in parallel after fetch; one agent each, signals inherited from offers.
            {"id": "write-a", "agent": "news-writer", "offer": "evening-write-a", "after": ["fetch"],
             "description": {"fi_FI": "Kirjoita Desk A:n kategorioiden artikkelit.",
                             "en_US": "Write the Desk A category articles."},
             "stage": ("crewaimeat.write_pipeline", "write_edition_articles"), "desk": "A"},
            {"id": "write-b", "agent": "news-writer-b", "offer": "evening-write-b", "after": ["fetch"],
             "description": {"fi_FI": "Kirjoita Desk B:n kategorioiden artikkelit.",
                             "en_US": "Write the Desk B category articles."},
             "stage": ("crewaimeat.write_pipeline", "write_edition_articles"), "desk": "B"},
            # Independent: pulls NOAA/NASA itself, writes the avaruussaa article into the set.
            {"id": "space-weather", "agent": "space-weather-writer", "offer": "space-weather",
             "description": {"fi_FI": "Avaruussää-artikkeli (NOAA/NASA).",
                             "en_US": "Space-weather article (NOAA/NASA)."}},
            {"id": "features", "agent": "daily-features-writer", "offer": "evening-features",
             "after": ["write-a", "write-b"],
             "description": {"fi_FI": "Erikoisosiot + uutisvisa päivän artikkeleista.",
                             "en_US": "Features + news quiz from the day's articles."},
             "stage": ("crewaimeat.features_pipeline", "build_quiz")},
            {"id": "editorial", "agent": "editorial-writer", "offer": "evening-editorial",
             "after": ["write-a", "write-b", "space-weather"],
             "retry": {"max": 2, "backoff_min": 5},
             "description": {"fi_FI": "Gonzo-pääkirjoitus + julkinen etusivuindeksi.",
                             "en_US": "Gonzo editorial + public front-page index."},
             "stage": ("crewaimeat.editorial_pipeline", "build_editorial_and_index")},
        ],
        "on_step_fail": "inspect",
    },
}


def node_definition(wf_id: str = "laimeat-sanomat-evening") -> dict:
    """Emit the exact `definition` payload for aimeat_workflow_save: localized title/description,
    one schedule trigger, typed vars, and steps as {id, agent, offer, after, description, retry}.
    Signals are INHERITED from each agent's offer (the node resolves+pins them), so they are NOT
    repeated here. Crew-side-only keys (`stage`, `desk`) are stripped — the node never sees them.
    `required_to_function:"none"` is left to the offer (fetch/space-weather offers declare it)."""
    wf = WORKFLOWS[wf_id]
    steps = []
    for s in wf["steps"]:
        step = {"id": s["id"], "agent": s["agent"], "offer": s["offer"],
                "description": s["description"]}
        if s.get("after"):
            step["after"] = s["after"]
        if s.get("retry"):
            step["retry"] = s["retry"]
        # A pure source step (offer omits required_to_function) declares "none" at the STEP level —
        # the only place the node accepts the bare string (it means "no input gate, don't block").
        if AGENT_SIGNALS.get(s["offer"], {}).get("required_to_function") == "none":
            step["required_to_function"] = "none"
        steps.append(step)
    vars_out = [{"name": v["name"], "type": v["type"], "description": v["description"],
                 **({"default": v["default"]} if "default" in v else {}),
                 **({"example": v["example"]} if "example" in v else {})}
                for v in wf["vars"]]
    return {
        "title": wf["title"],
        "description": wf["description"],
        "trigger": {"kind": "schedule", "cron": wf["schedule"]["cron"],
                    "timezone": wf["schedule"]["timezone"]},
        "vars": vars_out,
        "steps": steps,
        "on_step_fail": wf.get("on_step_fail", "inspect"),
    }


def resolve_step_signals(step: dict) -> tuple[Any, Any]:
    """Effective (required_to_function, success_signal): offer defaults overridden by the step."""
    base = AGENT_SIGNALS.get(step.get("offer") or "", {})
    req = step.get("required_to_function", base.get("required_to_function", "none"))
    succ = step.get("success_signal", base.get("success_signal"))
    return req, succ


def loc(localized: Any, locale: str = "fi_FI") -> str:
    if isinstance(localized, dict):
        return localized.get(locale) or next(iter(localized.values()), "")
    return localized or ""


def check_workflow(wf_id: str, params: dict, *, agent: str = "news-fetcher",
                   lister=None, llm_judge=None) -> dict:
    """Signals-only test run: evaluate BOTH signals of every step against existing memory.
    Returns {workflow, params, steps:[{id, state, input, output}]}. No dispatch, no LLM unless
    a judge is wired. `state` ∈ GREEN | input-RED | output-RED."""
    wf = WORKFLOWS[wf_id]
    vars = {v["name"]: params.get(v["name"], v.get("default")) for v in wf["vars"]}
    vars.update(params)
    lister = lister or _default_reader(agent)
    steps_out = []
    for step in wf["steps"]:
        req, succ = resolve_step_signals(step)
        in_ok, in_obs = check_signal(req, vars, lister, llm_judge)
        out_ok, out_obs = check_signal(succ, vars, lister, llm_judge)
        state = "GREEN" if (in_ok and out_ok) else ("input-RED" if not in_ok else "output-RED")
        steps_out.append({"id": step["id"], "state": state,
                          "input": {"ok": in_ok, "observed": in_obs},
                          "output": {"ok": out_ok, "observed": out_obs}})
    return {"workflow": wf_id, "params": vars, "steps": steps_out}
