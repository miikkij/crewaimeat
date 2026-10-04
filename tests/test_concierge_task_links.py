"""On the TASK path the concierge's files reach the person as public links; nothing claims an attachment.

Sold place, 2026-10-04: asked as a task "Tee minulle kuva: punainen omena valkoisella taustalla. Kerro
lopuksi kuvan osoite.", the deliverable said the image was attached and "not at a public URL" -- but a
task's deliverable is text on a task list, nothing attached reaches it, and the picture was in public
storage the whole time. These tests run the task path on a place whose node address is loopback.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from crewaimeat import node_ai, public_url

LOOPBACK = "http://127.0.0.1:40050"
PUBLIC = "https://koeajo.aimeat.io"
REQUEST = "Tee minulle kuva: punainen omena valkoisella taustalla. Kerro lopuksi kuvan osoite."


@pytest.fixture
def concierge():
    path = Path(__file__).resolve().parents[1] / "crews" / "concierge_crew.py"
    spec = importlib.util.spec_from_file_location("concierge_task_links_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._TASK_LINKS.clear()
    return mod


@pytest.fixture
def place(monkeypatch, concierge):
    """A sold place: node road, the node stores the picture and answers with a path, the credential's node
    address is loopback, and the fleet passes the public address. Any attach is a failure."""
    from crewaimeat import aimeat_crew, seedream_gen

    monkeypatch.setenv("AIMEAT_BASE_URL", PUBLIC)
    monkeypatch.setattr(public_url, "_node_url", lambda agent: LOOPBACK)
    monkeypatch.setattr(node_ai, "_on_node_road", lambda who: True)
    monkeypatch.setattr(seedream_gen, "_aimeat_call", lambda *a, **k: {"ok": True})
    image = {
        "storage_key": "images/20261003-234359-000fc2cee6",
        "mime_type": "image/jpeg",
        "size": 617000,
        "model": "bytedance-seed/seedream-4.5",
        "url": "/v1/pub/koeajo%40place/images/20261003-234359-000fc2cee6",
    }
    monkeypatch.setattr(aimeat_crew, "_aimeat_rest", lambda agent, method, path, body=None, **kw: image)

    def no_attach(*a, **k):
        raise AssertionError("the task path attached a file")

    monkeypatch.setattr(concierge.dm, "dm_attach_bytes", no_attach)
    return f"{PUBLIC}/v1/pub/koeajo%40place/images/20261003-234359-000fc2cee6"


def _task_tools(concierge, ctx_task_id="t-1"):
    from types import SimpleNamespace

    ctx = SimpleNamespace(task={"id": ctx_task_id}, prompt=REQUEST, llm=None, today="2026-10-04", identity="concierge")
    from crewai import Agent

    captured = {}
    real_agent = concierge._agent

    def agent(llm, sink, **kw):
        captured["sink"] = sink
        return Agent(
            role="Concierge", goal="g", backstory="b", tools=concierge._concierge_tools(sink), allow_delegation=False
        )

    concierge._agent = agent
    try:
        agents, tasks = concierge.build_domain(ctx)
    finally:
        concierge._agent = real_agent
    return captured["sink"], {t.name: t for t in agents[0].tools}, tasks[0]


def test_the_generated_image_goes_out_as_its_public_link(place, concierge):
    sink, tools, _task = _task_tools(concierge)
    out = tools["generate_image"].run(description="punainen omena valkoisella taustalla")
    assert place in out and "NOT attached" in out
    assert "127.0.0.1" not in out
    assert sink["attachments"] == [] and sink["links"] == [{"name": "generated image", "link": place}]


def test_the_publish_step_puts_a_link_the_reply_left_out_into_it(place, concierge):
    _sink, tools, _task = _task_tools(concierge)
    tools["generate_image"].run(description="omena")
    reply = "Tässä on sinulle luotu kuva punaisesta omenasta."
    out = concierge._links_into_reply(reply)
    assert out.startswith(reply) and place in out
    assert concierge._TASK_LINKS == {}, "taken once"


def test_a_link_the_reply_already_carries_is_not_repeated(place, concierge):
    _sink, tools, _task = _task_tools(concierge)
    tools["generate_image"].run(description="omena")
    reply = f"Tässä kuva. Kuvan osoite: {place}"
    assert concierge._links_into_reply(reply) == reply


def test_the_task_prompt_says_nothing_can_be_attached(concierge):
    _sink, _tools, task = _task_tools(concierge)
    assert "nothing can be attached" in task.description and "never say that something is attached" in task.description
    assert "Attach images/files with the tools" not in task.description


def test_the_dm_prompt_still_attaches(concierge):
    t = concierge._task(REQUEST, "", None, "2026-10-04")
    assert "Attach images/files with the tools" in t.description


def test_a_file_from_the_web_goes_out_as_its_source_address(place, concierge, monkeypatch):
    sink, tools, _task = _task_tools(concierge)
    monkeypatch.setattr(concierge, "_fetch_url_bytes", lambda url, **kw: (b"%PDF", "application/pdf", "lomake.pdf"))
    out = tools["fetch_file"].run(url="https://example.org/lomake.pdf")
    assert "https://example.org/lomake.pdf" in out and "NOT attached" in out
    assert sink["links"] == [{"name": "lomake.pdf", "link": "https://example.org/lomake.pdf"}]


def test_without_a_public_address_nothing_is_generated_and_nothing_is_claimed(concierge, monkeypatch):
    monkeypatch.delenv("AIMEAT_BASE_URL", raising=False)
    monkeypatch.setattr(public_url, "_node_url", lambda agent: LOOPBACK)
    monkeypatch.setattr(node_ai, "_on_node_road", lambda who: True)
    _sink, tools, _task = _task_tools(concierge)
    out = tools["generate_image"].run(description="omena")
    assert "public address is unknown" in out and "attached" not in out.lower()


def test_on_the_dm_path_a_failed_attach_gives_the_public_link(place, concierge, monkeypatch):
    monkeypatch.setattr(concierge.dm, "dm_attach_bytes", lambda *a, **k: None)
    monkeypatch.setattr(concierge, "_download", lambda url, **kw: (b"jpg", "image/jpeg", "k.jpg"))
    tool = next(t for t in concierge._concierge_tools({"attachments": []}) if t.name == "generate_image")
    out = tool.run(description="omena")
    assert "Could not attach" in out and place in out
