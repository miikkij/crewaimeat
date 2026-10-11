"""An offer's `example` is one real request — and every place that writes one says so, or is one.

When an agent proposal is approved, the node gives the new agent one task at once, and the task's
text is the `example` of the definition's first offer (aimeat-protocol 3f644989c). So the example
is the first request the agent RUNS. A model asked for `"example": "..."` and nothing more writes a
catalogue line — "Topic: 'X' → a short digest, each with its source" — which as a task is a request
with its answer attached. Two things are pinned here:

  * every prompt that asks a model for an offer example carries the one wording
    (`forge_catalog.OFFER_EXAMPLE_RULE`), and the guidance a person reads says the same;
  * every example this repository ships inside a definition template is a request only.
"""

import json
import pathlib

import pytest

from crewaimeat import brain_json, brain_templates, forge, forge_catalog, forge_json

ROOT = pathlib.Path(__file__).resolve().parent.parent
RULE = forge_catalog.OFFER_EXAMPLE_RULE


def test_the_wording_says_what_an_example_is():
    for part in (
        "one real request",
        "as a person would send it",
        "a sample of what they would give",
        "never the result",
    ):
        assert part in RULE, part
    assert "Morning Coffee" in RULE, "it shows one"


def test_the_crew_definition_prompt_tells_the_model_what_to_write_as_the_example():
    brief = forge_json.render_schema_brief()
    assert f'"example": "<{RULE}>"' in brief
    assert '"example": "..."' not in brief


def test_the_brain_template_prompt_tells_the_model_what_to_write_as_the_example():
    brief = brain_json.render_template_schema_brief()
    assert f'"example":"<{RULE}>"' in brief
    assert '"example":"..."' not in brief


def test_the_architects_output_format_tells_the_model_what_to_write_as_the_example():
    """crew-forge's Architect fills OFFER_EXAMPLE. The task text is built inside build_domain, so the
    source is what is read: the line takes the shared wording and no longer its own."""
    src = (ROOT / "crews" / "crew_forge_crew.py").read_text(encoding="utf-8")
    assert 'f"OFFER_EXAMPLE: <{forge_catalog.OFFER_EXAMPLE_RULE}' in src
    assert "one concrete example request" not in src


def test_the_build_tool_describes_the_example_it_takes():
    tools = {getattr(t, "name", ""): t for t in forge.make_forge_tools()}
    described = " ".join(
        (getattr(t, "description", "") or "")
        for t in tools.values()
        if "offer_example" in str(getattr(t, "description", ""))
    )
    assert described, "no tool describes offer_example"
    flat = " ".join(described.split())
    assert "one real request" in flat and "the request only, never the result" in flat


@pytest.mark.parametrize("doc", ["skills/aimeat-offer-authoring/SKILL.md", "CREW_AUTHORING_PROMPT.md"])
def test_the_guidance_says_the_same(doc):
    text = " ".join((ROOT / doc).read_text(encoding="utf-8").split())
    assert "the request only, never the result" in text


# --- the examples this repository ships in a definition ------------------------------------------- #


def _shipped_examples() -> dict[str, str]:
    found: dict[str, str] = {}
    for name in dir(brain_templates):
        value = getattr(brain_templates, name)
        if name.endswith("_OFFER") and isinstance(value, dict):
            found[f"brain_templates.{name}"] = value.get("example", "")
    for path in sorted((ROOT / "src" / "crewaimeat" / "agency" / "templates").glob("*.json")):
        offer = (json.loads(path.read_text(encoding="utf-8")).get("template") or {}).get("offer") or {}
        found[f"agency/templates/{path.name}"] = offer.get("example", "")
    return found


def test_there_are_shipped_examples_to_check():
    assert len(_shipped_examples()) >= 9, sorted(_shipped_examples())


@pytest.mark.parametrize("where,example", sorted(_shipped_examples().items()))
def test_a_shipped_example_is_a_request_and_carries_no_result(where, example):
    assert example.strip(), f"{where}: a definition's first offer needs an example; it is the agent's first task"
    assert "→" not in example and "->" not in example, f"{where}: the result is attached to the request: {example!r}"
    assert (
        not example.lstrip().lower().startswith(("topic:", "topics:", "question:", "url:", "company:", "location:"))
    ), f"{where}: a field label is not how a person asks: {example!r}"
