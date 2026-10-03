"""The crew forge offers image generation by the owner's ROAD, not by a key in this machine's environment.

Brief doc-muqud1ahihw8 step 3 takes the place's OPENROUTER_API_KEY out of the container. Images already go
through the node on the node road (crewaimeat.node_ai), but the forge's catalog gated image generation on
the key alone, so a fleet without it would stop offering images to new crews even where the node serves
them. The rule now:

  key on this machine                                        -> offered (the machine road)
  no key, owner's road is the node, node reports image on    -> offered (through the node)
  no key, owner's road is the node, node reports image off   -> not offered, with the node's reason
  no key, owner's road is not the node                       -> not offered, naming both ways
"""

from __future__ import annotations

import pytest

from crewaimeat import forge_catalog


class Node:
    def __init__(self, image_on=True, reason="NO_MODEL", message="No image model is configured."):
        self.calls: list[tuple[str, str]] = []
        self.answer = {
            "capabilities": {
                "image": {"on": True, "model": "seedream"}
                if image_on
                else {"on": False, "reason": reason, "message": message},
                "text": {"on": True},
            }
        }

    def rest(self, agent, method, path, body=None, **kw):
        self.calls.append((agent, path))
        return self.answer


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    forge_catalog._NODE_ANSWERS.clear()
    yield
    forge_catalog._NODE_ANSWERS.clear()


@pytest.fixture
def road(monkeypatch):
    """install(node_road, node) -> the stand-in node, with the owner's default road set."""
    from crewaimeat import aimeat_crew, llm_choice

    def install(node_road: bool, node: Node | None = None) -> Node:
        node = node or Node()
        monkeypatch.setattr(llm_choice, "default_is_node_road", lambda agent: node_road)
        monkeypatch.setattr(aimeat_crew, "_aimeat_rest", node.rest)
        return node

    return install


def _ids(asker="crew-forge"):
    return [c.id for c in forge_catalog.available_capabilities(asker=asker)]


def test_no_key_and_the_node_road_with_images_on_offers_image_generation(no_key, road):
    node = road(True)
    assert "image" in _ids()
    assert node.calls == [("crew-forge", "/v1/ai/capabilities")], "asked as the forging agent"
    usable, dropped = forge_catalog.resolve("image web", asker="crew-forge")
    assert "image" in usable and "image" not in dropped
    brief = forge_catalog.render_catalog_brief(forge_catalog.available_capabilities(asker="crew-forge"))
    assert "through the node" in brief and "needs env OPENROUTER_API_KEY" not in brief


def test_no_key_and_no_node_road_does_not_offer_it_and_names_both_ways(no_key, road):
    node = road(False)
    assert "image" not in _ids()
    ok, why = forge_catalog.preflight(forge_catalog.get("image"), asker="crew-forge")
    assert not ok and "OPENROUTER_API_KEY" in why and "not the node" in why
    assert node.calls == [], "off the node road the node is not asked"
    assert forge_catalog.resolve("image", asker="crew-forge") == ([], ["image"])


def test_the_node_road_without_an_image_model_does_not_offer_it_and_says_the_nodes_words(no_key, road):
    road(True, Node(image_on=False))
    ok, why = forge_catalog.preflight(forge_catalog.get("image"), asker="crew-forge")
    assert not ok and "No image model is configured" in why


def test_a_refused_capability_read_is_not_a_road(no_key, road):
    node = Node()
    node.answer = {"ok": False, "error": {"code": "SCOPE_DENIED", "message": "This needs ai:use."}}
    road(True, node)
    ok, why = forge_catalog.preflight(forge_catalog.get("image"), asker="crew-forge")
    assert not ok and "SCOPE_DENIED" in why


def test_the_key_on_this_machine_still_offers_it_without_asking_the_node(monkeypatch, road):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    forge_catalog._NODE_ANSWERS.clear()
    node = road(False)
    assert "image" in _ids()
    assert node.calls == []


def test_one_forge_run_asks_the_node_once(no_key, road):
    node = road(True)
    _ids()
    forge_catalog.resolve("image", asker="crew-forge")
    forge_catalog.preflight(forge_catalog.get("image"), asker="crew-forge")
    assert len(node.calls) == 1


def test_with_no_agent_to_ask_as_it_is_not_offered(no_key, road, monkeypatch):
    from crewaimeat import node_ai

    road(True)
    monkeypatch.setattr(node_ai, "agent_of", lambda a: None)
    ok, why = forge_catalog.preflight(forge_catalog.get("image"))
    assert not ok and "no agent to ask the node as" in why


def test_capabilities_without_a_node_capability_keep_their_env_gate(no_key, road):
    road(True)
    plain = [c for c in forge_catalog.CATALOG if c.env_required and not c.node_capability]
    for cap in plain:
        if not all(__import__("os").getenv(e) for e in cap.env_required):
            assert forge_catalog.preflight(cap, asker="crew-forge")[0] is False


def test_the_app_deploy_validator_follows_the_road_too(no_key, road):
    """A JSON crew-def naming the image tool validates on the node road with no key here."""
    road(True)
    ok, _why = forge_catalog.preflight(forge_catalog.get("image"))  # asker from the kickoff context
    # Outside a kickoff there is no agent to ask as; the validator runs inside one.
    assert ok is False
    ok, _why = forge_catalog.preflight(forge_catalog.get("image"), asker="deploy-app-agent")
    assert ok is True
