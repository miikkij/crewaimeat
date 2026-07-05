"""brain_json — data-driven agency TEMPLATES: a brain template as JSON, interpreted (not compiled).

An aimeat-agency ``brain_templates.Template`` is a working crew skeleton whose ``build(ctx, brain)`` is
Python — so adding a new template means editing code and recompiling. This module makes a template DATA:
a JSON file (a small template HEADER + a ``crewaimeat.crew_def`` crew doc) that loads into an ordinary
``Template`` whose ``.build`` is the generic interpreter. Add / edit / AI-author a template = edit JSON;
nothing is compiled, and ``validate_crew_doc`` proves it's runnable BEFORE it ever reaches a live brain.

The one thing a template needs beyond a plain crew def is the agency's user-editable layer: the operator's
standing ``prose`` and the run-time ``policy`` (publish key / visibility). The bridge substitutes those as
``{{brain.prose}}`` / ``{{brain.publish_key}}`` / ``{{brain.visibility}}`` (computed from the brain exactly
as the Python templates do, via ``brain_templates._run_inputs``), and lets ``crew_def`` inject the per-run
``{{ctx.prompt}}`` / ``{{ctx.today}}`` — so a JSON template reads just like the hand-written ones.

    from crewaimeat import brain_json
    brain_json.register_builtin_json_templates()   # load crew_defs/templates/*.json into the gallery
    ok, tj, errs = brain_json.generate_brain_template("watch a topic and email me a weekly digest", llm=...)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from crewaimeat.crew_def import CrewDocError, build_domain_from_json, render_tool_catalog, validate_crew_doc

# Brain-var placeholders a TEMPLATE may use (substituted from the live brain before interpretation). The
# per-run {{ctx.prompt}} / {{ctx.today}} are left for crew_def to inject at build time.
_BRAIN_VAR_RE = re.compile(r"\{\{\s*(brain\.\w+)\s*\}\}")
_STUB_VARS = {
    "brain.prose": "(operator prose)",
    "brain.publish_key": "answers.stub-agent.latest",
    "brain.visibility": "owner",
    "brain.agent_name": "stub-agent",
}


def _sub_brain_vars(text: str, vars: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key not in vars:
            raise CrewDocError([f"unknown template placeholder {{{{{key}}}}} (known brain vars: {sorted(vars)})"])
        return str(vars[key])

    return _BRAIN_VAR_RE.sub(repl, text)


def _apply_brain_vars(obj: Any, vars: dict[str, str]) -> Any:
    """Deep-substitute every ``{{brain.X}}`` in the doc's strings. Fails loud on an unknown brain var."""
    if isinstance(obj, str):
        return _sub_brain_vars(obj, vars)
    if isinstance(obj, list):
        return [_apply_brain_vars(x, vars) for x in obj]
    if isinstance(obj, dict):
        return {k: _apply_brain_vars(v, vars) for k, v in obj.items()}
    return obj


def _brain_vars(ctx: Any, header: dict, brain: dict) -> tuple[dict[str, str], str]:
    """(brain-var map, this-run request) computed exactly like the Python templates (reuses
    brain_templates._run_inputs so publish-key/visibility rules never drift)."""
    from crewaimeat import brain_templates as bt

    agent_name = brain["agent_name"]
    default_base = f"{header.get('default_publish_base') or 'watch'}.{agent_name}"
    prose, request, visibility, publish_key = bt._run_inputs(ctx, brain, header.get("default_prose", ""), default_base)
    vars = {
        "brain.prose": prose,
        "brain.publish_key": publish_key,
        "brain.visibility": visibility,
        "brain.agent_name": agent_name,
    }
    return vars, request


def build_from_json_template(doc: dict, header: dict, ctx: Any, brain: dict) -> tuple[list, list]:
    """A ``Template.build`` over a JSON crew doc: substitute the brain's prose/policy vars, set the crew's
    identity to the brain's agent name, then interpret it into (agents, tasks) with the per-run request as
    ``{{ctx.prompt}}``. Drop-in equivalent of a hand-written template ``build``."""
    vars, request = _brain_vars(ctx, header, brain)
    built = _apply_brain_vars(doc, vars)
    built = dict(built)
    built["agent_name"] = brain["agent_name"]  # a template omits agent_name; it is per-brain
    eff_ctx = SimpleNamespace(
        llm=getattr(ctx, "llm", None),
        prompt=request,
        today=getattr(ctx, "today", "") or "",
        directives=getattr(ctx, "directives", "") or "",
        task=getattr(ctx, "task", None) or {},
    )
    return build_domain_from_json(built, eff_ctx)


def validate_template(tj: Any) -> list[str]:
    """Return the problems with a JSON template ({template: <header>, crew: <crew doc>}) — empty if valid.
    Validates the crew doc as it will run (stub brain vars substituted, a stub agent_name set), so a bad
    template is caught at author/generate time, before it can become a live brain."""
    if not isinstance(tj, dict):
        return ["template must be a JSON object with 'template' + 'crew'"]
    errors: list[str] = []
    header = tj.get("template")
    doc = tj.get("crew")
    if not isinstance(header, dict):
        errors.append("template: a 'template' header object is required")
    else:
        for f in ("id", "title"):
            if not (isinstance(header.get(f), str) and header[f].strip()):
                errors.append(f"template.{f}: required non-empty string")
    if not isinstance(doc, dict):
        errors.append("crew: a 'crew' crew-def object is required")
        return errors
    try:
        built = _apply_brain_vars(doc, _STUB_VARS)
    except CrewDocError as exc:
        return errors + exc.errors
    built = dict(built)
    built["agent_name"] = "stub-agent"  # a template omits agent_name; supply one for validation
    errors.extend(validate_crew_doc(built))
    return errors


def template_from_json(tj: dict):
    """Turn a JSON template into a live ``brain_templates.Template`` (its ``.build`` is the bridge above).
    Raises ``CrewDocError`` if the template is invalid — a bad template never reaches the gallery."""
    from crewaimeat import brain_templates as bt

    errs = validate_template(tj)
    if errs:
        raise CrewDocError(errs)
    header = tj["template"]
    doc = tj["crew"]

    def build(ctx: Any, brain: dict) -> tuple[list, list]:
        return build_from_json_template(doc, header, ctx, brain)

    return bt.Template(
        id=header["id"],
        title=header["title"],
        description=header.get("description", ""),
        default_prose=header.get("default_prose", ""),
        default_policy=header.get("default_policy") or bt._default_policy(),
        build=build,
        policy_fields=header.get("policy_fields") or bt._STD_POLICY_FIELDS,
        i18n=header.get("i18n") or {},
        offer=header.get("offer"),
    )


def load_json_templates(directory: str | Path) -> list:
    """Load + register every ``*.json`` template in ``directory`` into the ``brain_templates`` gallery.
    A file that fails validation is SKIPPED with a loud log (one bad template never breaks the gallery).
    Returns the registered ``Template`` objects."""
    from crewaimeat import brain_templates as bt

    p = Path(directory)
    if not p.is_dir():
        return []
    out = []
    for f in sorted(p.glob("*.json")):
        try:
            tj = json.loads(f.read_text(encoding="utf-8"))
            t = template_from_json(tj)
        except (ValueError, CrewDocError) as exc:
            detail = "; ".join(exc.errors) if isinstance(exc, CrewDocError) else str(exc)
            print(f"[brain_json] skipping template {f.name}: {detail}", file=sys.stderr)
            continue
        bt.register(t)
        out.append(t)
    return out


def default_templates_dir() -> Path:
    """The repo's built-in JSON-template dir (``crew_defs/templates/``)."""
    from crewaimeat.forge import _project_root

    return _project_root() / "crew_defs" / "templates"


def register_builtin_json_templates() -> list:
    """Load the built-in JSON templates (``crew_defs/templates/*.json``) into the gallery. Call this once
    at agency startup, after the Python templates register — a JSON file with a new id ADDS a template; one
    reusing a Python template's id replaces it (data wins), so a template can migrate code -> data safely."""
    return load_json_templates(default_templates_dir())


# --------------------------------------------------------------------------------------------------
# AI authoring — turn a plain-language description into a validated JSON template. The model output is
# checked with validate_template BEFORE it is saved, so an AI-authored brain is provably runnable (no
# compile, no crash-on-load) — the whole point of the data-driven template.
# --------------------------------------------------------------------------------------------------
def render_template_schema_brief() -> str:
    """The spec an AI author writes to: the JSON-template shape + brain-var rules + the tool menu (only
    tools the interpreter can resolve, from crew_def.TOOL_REGISTRY)."""
    return (
        "Design an agency BRAIN TEMPLATE as a SINGLE JSON object with two parts:\n"
        "{\n"
        '  "template": {\n'
        '    "id": "<kebab-id>", "title": "...", "description": "one line: what this kind of agent does",\n'
        '    "default_prose": "the standing instructions the operator will edit (what it should do)",\n'
        '    "default_publish_base": "<kebab base for published results, e.g. answers|watch|briefing>",\n'
        '    "offer": {"id":"<kebab>","title":"...","ask":"what to send + what it returns + what it does NOT do",'
        ' "example":"...","cost":"cheap","latency":"minutes","repeatability":"idempotent","verification":"ungated","consequences":[]}\n'
        "  },\n"
        '  "crew": {\n'
        '    "temperature": <0.25 factual | 0.5 mixed | 0.7 creative>,\n'
        '    "tags": ["<kebab subject words>", "role.task-runner"],\n'
        '    "agents": [{"name":"<local-id>","role":"...","goal":"...","backstory":"...","tools":["<ids>"]}],\n'
        '    "tasks":  [{"id":"<local-id>","agent":"<a name>","description":"...","expected_output":"..."}]\n'
        "  }\n"
        "}\n\n"
        "RULES:\n"
        '- OMIT agent_name in "crew" — it is set per brain.\n'
        "- In task descriptions use these placeholders (nothing else):\n"
        "    {{brain.prose}}      the operator's standing instructions\n"
        "    {{ctx.prompt}}       the per-run request/question (at least one task MUST include this)\n"
        "    {{ctx.today}}        current date/time (prepend to time-sensitive tasks)\n"
        "    {{brain.publish_key}} and {{brain.visibility}}  when the task publishes its result upward\n"
        "- EVERY task needs a string description + expected_output, and an agent matching an agent's name.\n"
        '- Chain with "context": [<earlier task ids>] (a DAG); the LAST task\'s output is the deliverable.\n\n'
        "AVAILABLE TOOL IDS (attach only what the job needs):\n" + render_tool_catalog()
    )


def _generation_prompt(description: str) -> str:
    return (
        "You are designing a reusable aimeat-agency brain template from a plain-language description.\n\n"
        f"DESCRIPTION:\n{description}\n\n"
        f"{render_template_schema_brief()}\n\n"
        "Output EXACTLY the single JSON object and nothing else — no prose, no code fences."
    )


def generate_brain_template(description: str, *, llm: Any = None) -> tuple[bool, dict | None, list[str]]:
    """Turn a description into a VALIDATED JSON template. Returns ``(ok, template_json, errors)``. The
    model's output is coerced (tolerating fences/prose) and checked with ``validate_template`` before it
    is returned, so a caller can save it straight to the gallery only when ``ok``. Pass ``llm`` (a crewai
    ``BaseLLM`` with ``.call``); defaults to the agency's completion LLM."""
    from crewaimeat.forge_json import coerce_doc

    if llm is None:
        from crewaimeat.llm import get_llm

        llm = get_llm(for_tool_use=False)
    try:
        raw = llm.call(_generation_prompt(description))
    except Exception as exc:  # noqa: BLE001 — a model/transport error is reported, not raised, to the caller
        return False, None, [f"generation failed: {type(exc).__name__}: {exc}"]
    tj = coerce_doc(raw)
    if tj is None:
        return False, None, ["the model did not return a JSON object"]
    errs = validate_template(tj)
    return (not errs), (tj if not errs else None), errs
