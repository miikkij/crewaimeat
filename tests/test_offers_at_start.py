"""A changed offer reaches the node when the fleet starts, not only on the agent's next worker run.

Measured 2026-10-16: julkaisu-toimittaja gained the offer `vie-lahettimeen`, the fleet was restarted,
and the node still held only `valitse-aihe` (its last worker ran on 2026-09-14), so saving the
workflow step that names the new offer was refused: `agent "julkaisu-toimittaja" does not publish
offer "vie-lahettimeen"`. Offers were published only at a worker's own start, and a spawn-mode agent
nobody wakes never starts one.

Now the spawner, at its own start, hands every crew-file agent it serves to one short process
(`python -m crewaimeat.offers --publish-changed ...`), which publishes an agent's offers only when the
document built from its crew file differs from what the node holds, or the node holds none.
"""

from __future__ import annotations

import json

import pytest

from crewaimeat import offers

GAII = "julkaisu-toimittaja#happydude500001@aimeat-finland-001-genesis"

OFFER = {
    "id": "vie-lahettimeen",
    "title": "Vie hyväksytyt tekstit LÄHETTIMEEN luonnoksiksi",
    "deliverable": {"format": "document", "sample": "untested", "location": {"key": "julkaisu.{ref}.lahetin"}},
    "latency": "seconds",
}


def _held(*offers_):
    return {"offers": [json.loads(json.dumps(o)) for o in offers_], "version": 1}


# ── when the node's copy is out of date ─────────────────────────────────────────────────────


def test_the_same_offers_with_another_sample_are_not_a_change():
    held = _held({**OFFER, "deliverable": {**OFFER["deliverable"], "sample": {"live": "excerpt"}}})
    held["offers"][0]["availability"] = {"scheduleBorn": None, "boundToLastSeen": True}  # the node adds it
    assert offers.offers_differ([OFFER], held) is False


@pytest.mark.parametrize(
    "held",
    [
        None,  # the node holds no document
        {"offers": []},
        _held({**OFFER, "latency": "minutes"}),  # a field changed
        _held({**OFFER, "id": "valitse-aihe"}),  # another offer
    ],
)
def test_a_missing_or_changed_document_is_a_change(held):
    assert offers.offers_differ([OFFER], held) is True


def test_a_new_offer_beside_the_old_one_is_a_change():
    old = {**OFFER, "id": "valitse-aihe"}
    assert offers.offers_differ([old, OFFER], _held(old)) is True


# ── the one pass the spawner starts ─────────────────────────────────────────────────────────


class Node:
    def __init__(self, held: dict):
        self.held = held  # agent -> what the node answers for its offers key
        self.puts: list[tuple[str, str]] = []
        self.reads: list[tuple[str, str]] = []

    def call(self, agent, tool, payload, **kw):
        assert tool == "aimeat_memory_read"
        self.reads.append((agent, payload["key"]))
        name = agent.split("#", 1)[0]
        if name not in self.held:
            return {"ok": False, "error": {"code": "NOT_FOUND", "message": "no such key"}}
        return {"key": payload["key"], "value": self.held[name]}

    def rest(self, agent, method, path, body=None, **kw):
        self.puts.append((agent, path))
        return {"offers": body["offers"]}


@pytest.fixture
def node(monkeypatch):
    def make(held: dict, built: dict) -> Node:
        n = Node(held)
        monkeypatch.setattr(offers, "_aimeat_call", n.call)
        monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_rest", n.rest)

        def doc(agent, with_samples=False):
            if agent == "broken":
                raise RuntimeError("the crew file does not build")
            return {"version": 1, "updatedAt": "now", "offers": list(built.get(agent, []))}

        monkeypatch.setattr(offers, "offers_doc_any", doc)
        return n

    return make


def test_only_a_changed_agent_is_published(node):
    n = node(
        held={"julkaisu-toimittaja": _held({**OFFER, "id": "valitse-aihe"}), "postman": _held(OFFER)},
        built={"julkaisu-toimittaja": [{**OFFER, "id": "valitse-aihe"}, OFFER], "postman": [OFFER]},
    )
    out = offers.publish_changed([GAII, "postman"])
    assert out == {GAII: "published", "postman": "unchanged"}
    assert n.puts == [(GAII, "/v1/agents/julkaisu-toimittaja/offers")], "one PUT, as the agent, by its name"


def test_an_agent_the_node_holds_nothing_for_is_published(node):
    n = node(held={}, built={"postman": [OFFER]})
    assert offers.publish_changed(["postman"]) == {"postman": "published"}
    assert len(n.puts) == 1


def test_an_agent_without_offers_is_left_alone(node):
    n = node(held={}, built={})
    assert offers.publish_changed(["news-writer"]) == {"news-writer": "no offers"}
    assert n.puts == [] and n.reads == []


def test_one_agent_that_fails_does_not_stop_the_others(node):
    n = node(held={}, built={"postman": [OFFER]})
    out = offers.publish_changed(["broken", "postman"])
    assert out["broken"].startswith("FAILED") and "does not build" in out["broken"]
    assert out["postman"] == "published" and len(n.puts) == 1


def test_publish_offers_any_takes_the_full_identity(node):
    """It built the document by the crew-file name, so a GAII found no crew and published nothing."""
    n = node(held={}, built={"julkaisu-toimittaja": [OFFER]})
    assert offers.publish_offers_any(GAII, with_samples=False) is True
    assert n.puts == [(GAII, "/v1/agents/julkaisu-toimittaja/offers")]


# ── the spawner's half ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def own_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "home"))


def _crew(tmp_path, name, *, parked=False):
    crews = tmp_path / "crews"
    crews.mkdir(exist_ok=True)
    prefix = "_" if parked else ""
    (crews / f"{prefix}{name.replace('-', '_')}_crew.py").write_text(
        f'AGENT_NAME = "{name}"\n\ndef build_domain(ctx):\n    ...\n\ndef run():\n    ...\n', encoding="utf-8"
    )


def test_the_spawner_hands_its_crew_file_agents_to_one_publish_pass(tmp_path):
    from crewaimeat.spawner import Spawner

    _crew(tmp_path, "julkaisu-toimittaja")
    _crew(tmp_path, "old-agent", parked=True)
    passed: list = []
    node_defined = "gtm-mittari#happydude500001@aimeat-finland-001-genesis"
    sp = Spawner(
        agents=[GAII, node_defined, "old-agent"],
        root=tmp_path,
        spawn_fn=lambda a, r: pytest.fail("no worker is started for offers"),
        offers_fn=lambda root, agents: passed.append((root, list(agents))),
    )
    assert sp.publish_offers_at_start() == [GAII]
    assert passed == [(tmp_path, [GAII])], "crew-file agents only; a node-defined or parked one has no crew file offer"


def test_a_publish_pass_that_fails_does_not_stop_the_spawner(tmp_path, capsys):
    from crewaimeat.spawner import Spawner

    _crew(tmp_path, "julkaisu-toimittaja")

    def boom(root, agents):
        raise OSError("cannot start the process")

    sp = Spawner(agents=[GAII], root=tmp_path, offers_fn=boom)
    assert sp.publish_offers_at_start() == []
    assert "cannot start the process" in capsys.readouterr().err


def test_under_pytest_the_default_starts_no_process(tmp_path):
    from crewaimeat.spawner import launch_offer_publish

    assert launch_offer_publish(tmp_path, [GAII]) is None


@pytest.mark.local_process
def test_the_spawner_itself_does_not_import_crewai():
    import subprocess
    import sys

    probe = "import sys, crewaimeat.spawner; print('crewai' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)  # noqa: S603
    assert out.stdout.strip() == "False"
