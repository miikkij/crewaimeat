"""Images, vision and embeddings follow the owner's road (crewaimeat.node_ai).

Brief doc-muqud1ahihw8 step 3 was held because six call sites paid with this machine's
OPENROUTER_API_KEY, outside the node's metering, so an owner's own key never paid them. These tests hold
the rule every one of them now keeps:

  the node road -> the node's own door with the agent's credential, never this machine's key;
  not the node road, a key here -> the direct path, as before;
  neither -> a visible failure that names both ways out.

The node is a recording stand-in at the one seam the code calls through (`_aimeat_rest`).
"""

from __future__ import annotations

import pytest

from crewaimeat import node_ai


class Node:
    def __init__(self, answers=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.answers = answers or {}

    def rest(self, agent, method, path, body=None, **kw):
        self.calls.append((agent, path, body or {}))
        return self.answers.get(path, {})


@pytest.fixture
def on_node(monkeypatch):
    """The owner routes every agent through the node; any direct OpenRouter POST fails the test."""
    from crewaimeat import aimeat_crew

    monkeypatch.setattr(node_ai, "_on_node_road", lambda who: True)
    monkeypatch.setenv("AIMEAT_BASE_URL", "https://place.aimeat.io")
    node = Node(
        {
            "/v1/ai/image": {
                "storage_key": "images/x.png",
                "mime_type": "image/png",
                "size": 42,
                "model": "node-image-model",
                "url": "http://node/v1/pub/owner%23x%40n/images/x.png",
            },
            "/v1/ai/complete": {"content": "a red door", "model": "node-vision-model"},
            "/v1/ai/embed": {"embeddings": [[0.1, 0.2, 0.3]], "model": "text-embed-x", "dimensions": 3},
        }
    )
    monkeypatch.setattr(aimeat_crew, "_aimeat_rest", node.rest)
    import requests

    def no_direct(*a, **k):
        raise AssertionError(f"a direct provider call on the node road: {a[:1]}")

    monkeypatch.setattr(requests, "post", no_direct)
    return node


# ── the road ─────────────────────────────────────────────────────────────────────────────────


def test_the_road_is_the_node_when_the_owner_chose_it(monkeypatch):
    monkeypatch.setattr(node_ai, "_on_node_road", lambda who: True)
    assert node_ai.road("a") == node_ai.NODE


def test_off_the_node_road_a_key_here_is_the_machine_road(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert node_ai.road("a") == node_ai.MACHINE


def test_with_neither_there_is_no_road_and_the_sentence_names_both_ways_out(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert node_ai.road("a") == node_ai.NONE
    msg = node_ai.no_route("vision", "a")
    assert "not the node" in msg and "OPENROUTER_API_KEY" in msg


def test_a_road_that_cannot_be_read_is_said_and_not_guessed_as_the_node(monkeypatch, capsys):
    monkeypatch.setattr(node_ai, "_on_node_road", lambda who: (_ for _ in ()).throw(RuntimeError("no node")))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert node_ai.road("a") == node_ai.MACHINE
    assert "could not read the owner's model road" in capsys.readouterr().err


# ── each caller on the node road ─────────────────────────────────────────────────────────────


def test_image_generation_goes_to_the_nodes_image_door(on_node, monkeypatch):
    from crewaimeat import seedream_gen

    monkeypatch.setattr(seedream_gen, "_aimeat_call", lambda *a, **k: {"ok": True})
    out = seedream_gen.generate_image("painter", "a red door")
    assert out["ok"] and out["url"].endswith("images/x.png") and out["model"] == "node-image-model"
    assert out["gaii"] == "owner#x@n", "the address to attach by travels with the URL"
    [(agent, path, body)] = on_node.calls
    assert agent == "painter" and path == "/v1/ai/image" and body["public"] is True
    assert "model" not in body and "size" not in body, "the node picks the model; Seedream's size means nothing to it"


def test_concierge_vision_goes_to_the_node_with_the_image(on_node):
    from crewaimeat import vision

    assert vision.analyze_image(b"\x89PNG", "image/png", agent="concierge") == "a red door"
    [(agent, path, body)] = on_node.calls
    assert agent == "concierge" and path == "/v1/ai/complete"
    assert body["images"][0].startswith("data:image/png;base64,")


def test_moodboard_vision_goes_to_the_node(on_node):
    from crewaimeat import image_contract

    on_node.answers["/v1/ai/complete"] = {"content": '{"subject": "door", "relevance": 12}'}
    meta = image_contract._vision_meta(b"\x89PNG", "image/png", "doors")
    assert meta == {"subject": "door", "relevance": 10}
    assert on_node.calls[0][0] == image_contract.AGENT


def test_the_browser_describe_goes_to_the_node_as_its_agent(on_node, tmp_path):
    from crewaimeat import browser_tool

    shot = tmp_path / "s.png"
    shot.write_bytes(b"\x89PNG")
    assert browser_tool._describe_image(str(shot), "what is on the page", "web-tester") == "a red door"
    assert on_node.calls[0][:2] == ("web-tester", "/v1/ai/complete")


def test_memory_embeddings_come_from_the_node_and_the_store_is_named_by_its_model(on_node):
    from crewaimeat.embedder_cascade import resolve_embedder

    spec, tag = resolve_embedder("researcher")
    assert spec["provider"] == "custom" and tag == "node-text-embed-x-3"
    fn = spec["config"]["embedding_callable"](**{k: v for k, v in spec["config"].items() if k != "embedding_callable"})
    vecs = fn(["hello"])
    assert len(vecs) == 1 and list(vecs[0]) == pytest.approx([0.1, 0.2, 0.3])
    assert all(c[0] == "researcher" and c[1] == "/v1/ai/embed" for c in on_node.calls)


def test_the_memory_analysis_model_is_the_agents_own_node_road(on_node, monkeypatch):
    from crewaimeat import llm, pipeline_memory

    seen = {}
    monkeypatch.setattr(llm, "get_llm", lambda **kw: seen.update(kw) or "node-llm")
    assert pipeline_memory.default_analysis_llm("researcher", "node-x-3") == "node-llm"
    assert seen["agent_name"] == "researcher"


# ── refusals and the no-road case ────────────────────────────────────────────────────────────


def test_a_node_refusal_comes_back_in_the_nodes_words(on_node):
    from crewaimeat import vision

    on_node.answers["/v1/ai/complete"] = {
        "ok": False,
        "error": {"code": "SCOPE_DENIED", "message": "This needs ai:use."},
        "http_status": 403,
    }
    out = vision.analyze_image(b"\x89PNG", "image/png", agent="concierge")
    assert "SCOPE_DENIED" in out and "ai:use" in out


def test_a_memory_crew_on_the_node_road_does_not_run_without_its_embeddings(on_node):
    from crewaimeat.embedder_cascade import resolve_embedder

    on_node.answers["/v1/ai/embed"] = {"ok": False, "error": {"code": "NO_MODEL", "message": "no embed model"}}
    with pytest.raises(RuntimeError, match="NO_MODEL"):
        resolve_embedder("researcher")


def test_image_generation_is_not_retried_because_a_second_post_could_pay_twice(on_node, monkeypatch):
    from crewaimeat import aimeat_crew

    seen = {}

    def rest(agent, method, path, body=None, **kw):
        seen.update(kw)
        return {"url": "http://node/v1/pub/o/k", "storage_key": "k"}

    monkeypatch.setattr(aimeat_crew, "_aimeat_rest", rest)
    node_ai.generate_image("a", "p")
    assert seen["retries"] == 1


@pytest.mark.parametrize(
    "call",
    [
        lambda: __import__("crewaimeat.seedream_gen", fromlist=["x"]).generate_image("a", "p")["error"],
        lambda: __import__("crewaimeat.vision", fromlist=["x"]).analyze_image(b"x", "image/png", agent="a"),
    ],
)
def test_with_no_road_the_failure_is_visible(monkeypatch, call):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("AIMEAT_BASE_URL", "https://place.aimeat.io")
    assert "OPENROUTER_API_KEY is not set" in call()


def test_a_relative_image_address_from_the_node_is_made_whole_on_both_addresses(on_node, monkeypatch):
    """Measured 2026-10-03: /v1/ai/image answers with "/v1/pub/<owner>/<key>". The PERSON gets the place's
    public address in front; the crew's own re-fetch gets its node address, which works from inside."""
    from crewaimeat import public_url, seedream_gen

    on_node.answers["/v1/ai/image"] = dict(on_node.answers["/v1/ai/image"], url="/v1/pub/owner%23x%40n/images/x.png")
    monkeypatch.setattr(seedream_gen, "_aimeat_call", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(public_url, "_node_url", lambda agent: "http://127.0.0.1:40050")
    out = seedream_gen.generate_image("painter", "a red door")
    assert out["url"] == "https://place.aimeat.io/v1/pub/owner%23x%40n/images/x.png"
    assert out["fetch_url"] == "http://127.0.0.1:40050/v1/pub/owner%23x%40n/images/x.png"
    assert seedream_gen.drain_recent_images()[-1]["url"] == out["url"], "the deliverable carries the public one"


def test_with_no_public_address_no_image_is_generated(on_node, monkeypatch):
    """A picture nobody can open is not paid for: the check comes BEFORE the generation."""
    from crewaimeat import public_url, seedream_gen

    monkeypatch.delenv("AIMEAT_BASE_URL", raising=False)
    monkeypatch.setattr(public_url, "_node_url", lambda agent: "http://127.0.0.1:40050")
    out = seedream_gen.generate_image("painter", "a red door")
    assert out["ok"] is False and "public address is unknown" in out["error"]
    assert on_node.calls == [], "the node was not asked to generate"
