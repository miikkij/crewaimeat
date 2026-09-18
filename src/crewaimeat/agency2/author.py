"""Describe → a validated crew definition. The model writes the definition; the checking is code.

The spec the model gets is the one crew-forge's declarative path already uses
(`forge_json.render_schema_brief`: the shape, the rules the validator enforces, and the tool ids this
machine can actually run), and every answer goes through `crew_def.validate_crew_doc`. A failed
attempt's errors are handed back for the next one, and the BEST attempt is kept: an attempt never ends
worse than one before it (CLAUDE.md, the night of 2026-08-28).

The person never sees JSON: `summary()` turns a definition into plain sentences, deterministically.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

MAX_ATTEMPTS = 3


def _prompt(name: str, description: str, lang: str, *, current: dict | None, errors: list[str] | None) -> str:
    from crewaimeat.forge_json import render_schema_brief

    parts = [
        "You design an AIMEAT agent for a person who does not code. They described what they want in "
        "their own words; turn it into a crew definition that does exactly that.",
        f'The agent_name MUST be exactly "{name}".',
        f"The person writes in {'Finnish' if lang == 'fi' else 'English'}; the crew answers in the "
        "language of each request unless the description says otherwise.",
    ]
    if current is not None:
        parts += [
            "This agent ALREADY EXISTS. Change its current definition as the person asks and keep "
            "everything they did not ask to change.",
            "CURRENT DEFINITION:\n" + json.dumps(current, ensure_ascii=False, indent=1),
            "WHAT THE PERSON WANTS CHANGED:\n" + description,
        ]
    else:
        parts.append("WHAT THE PERSON WANTS:\n" + description)
    parts.append(render_schema_brief())
    if errors:
        parts.append(
            "YOUR PREVIOUS ANSWER WAS REJECTED by the validator. Fix exactly these and nothing else:\n- "
            + "\n- ".join(errors)
        )
    parts.append("Output EXACTLY the single JSON object and nothing else — no prose, no code fences.")
    return "\n\n".join(parts)


def _normalize(doc: dict, name: str) -> dict:
    doc = dict(doc)
    doc["agent_name"] = name
    tags = [t for t in (doc.get("tags") or []) if isinstance(t, str)]
    if "role.task-runner" not in tags:
        tags.append("role.task-runner")
    doc["tags"] = tags
    return doc


def author(
    name: str,
    description: str,
    *,
    lang: str = "fi",
    current: dict | None = None,
    llm: Any = None,
    call: Callable[[str], str] | None = None,
) -> dict:
    """Returns {ok, doc, errors, attempts}. `doc` is the best attempt (valid when ok).

    `errors` is for the PERSON: sentences with a `[ref:…]` to the whole cause (problems.py). The
    validator's own lines are for the model — they go back into the next attempt's prompt and into
    the problems log, never straight into what this returns."""
    from crewaimeat.agency2 import problems
    from crewaimeat.crew_def import validate_crew_doc
    from crewaimeat.forge_json import coerce_doc

    if call is None:
        if llm is None:
            from crewaimeat.agency2.engine import openrouter_only
            from crewaimeat.llm import get_llm

            openrouter_only()
            llm = get_llm(for_tool_use=False, temperature=0.3)
        call = llm.call
    model = _model_of(llm)

    best: tuple[int, dict | None, list[str]] = (10**6, None, ["no attempt produced a definition"])
    errors: list[str] | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            raw = call(_prompt(name, description, lang, current=current, errors=errors))
        except Exception as exc:  # noqa: BLE001 — a model/transport error is the answer, shown as is
            _log(name, attempt, model, t0, f"model call failed: {type(exc).__name__}")
            said = [problems.say(exc, "author.model_call", lang, kind="model")]
            if best[1] is not None:  # an earlier attempt is kept: never end worse than it
                said.append(problems.say_invalid(best[2], "author.validation", lang))
            return {"ok": False, "doc": best[1], "errors": said, "attempts": attempt}
        doc = coerce_doc(raw)
        if doc is None:
            errors = ["the answer was not a JSON object"]
            _log(name, attempt, model, t0, f"not JSON ({len(raw or '')} chars)")
            continue
        doc = _normalize(doc, name)
        errors = validate_crew_doc(doc)
        _log(name, attempt, model, t0, "valid" if not errors else f"{len(errors)} validation error(s)")
        if len(errors) < best[0]:
            best = (len(errors), doc, errors)
        if not errors:
            return {"ok": True, "doc": doc, "errors": [], "attempts": attempt}
    said = [problems.say_invalid(best[2], "author.validation", lang)]
    return {"ok": False, "doc": best[1], "errors": said, "attempts": MAX_ATTEMPTS}


def _model_of(llm: Any) -> str:
    if llm is None:
        return "?"
    try:
        from crewaimeat.llm import resolved_model

        return resolved_model(llm) or "?"
    except Exception:  # noqa: BLE001 — only a label for the log line
        return "?"


def _log(name: str, attempt: int, model: str, t0: float, outcome: str) -> None:
    """One line per model call — the duration and what came back are MEASURED, never guessed later."""
    print(f"[agency2] author {name} #{attempt} {model} {time.monotonic() - t0:.0f}s: {outcome}", flush=True)


_TOOL_WORDS = {
    "fi": {
        "web": "hakee verkosta",
        "memory": "lukee ja kirjoittaa AIMEAT-muistiin",
        "schedule": "ajastaa töitä",
        "delegate": "pyytää apua toisilta agenteilta",
        "image": "tekee kuvia",
        "app_build": "rakentaa AIMEAT-sovelluksia",
        "exchange": "käy kauppaa AIMEAT-pörssissä",
    },
    "en": {
        "web": "searches the web",
        "memory": "reads and writes AIMEAT memory",
        "schedule": "schedules work",
        "delegate": "asks other agents for help",
        "image": "makes images",
        "app_build": "builds AIMEAT apps",
        "exchange": "trades on the AIMEAT exchange",
    },
}


def summary(doc: dict, lang: str = "fi") -> dict:
    """Plain-language description of a definition: who does what, in order, and with which tools."""
    words = _TOOL_WORDS.get(lang, _TOOL_WORDS["en"])
    agents = {a.get("name"): a for a in doc.get("agents") or [] if isinstance(a, dict)}
    members = []
    for a in agents.values():
        tools = [words.get(t, t) for t in (a.get("tools") or [])]
        members.append({"role": a.get("role") or a.get("name"), "goal": a.get("goal") or "", "tools": tools})
    steps = []
    for t in doc.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        who = agents.get(t.get("agent"), {})
        steps.append({"who": who.get("role") or t.get("agent"), "delivers": t.get("expected_output") or ""})
    return {"members": members, "steps": steps}
