"""A link given to a PERSON uses the place's public address, never the crew's loopback connection to the node.

Sold place, 2026-10-03: asked for an image, the concierge answered
"Kuvan osoite: http://127.0.0.1:40050/v1/pub/koeavain055546%40aim-solo-882a01e3/images/20261003-192812-298639512c"
-- the crew's own connection to the node, which nobody outside the container can open. These tests put
every person-facing link builder on such a place (its credential's node address is loopback) and hold
that what comes out starts with the public address, or that no link is given at all.
"""

from __future__ import annotations

import pytest

from crewaimeat import public_url

LOOPBACK = "http://127.0.0.1:40050"
PUBLIC = "https://koe.aimeat.io"


@pytest.fixture
def place(monkeypatch):
    """A hosted place: the credential says loopback, the fleet passes the public address."""
    monkeypatch.setattr(public_url, "_node_url", lambda agent: LOOPBACK)
    monkeypatch.setenv("AIMEAT_BASE_URL", PUBLIC)


def _no_loopback(text: str | None) -> None:
    assert text is not None
    assert "127.0.0.1" not in text and "localhost" not in text, text


# ── the address rules ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "internal"),
    [
        ("http://127.0.0.1:40050/x", True),
        ("http://localhost:40050", True),
        ("http://[::1]:40050", True),
        ("http://0.0.0.0:40050", True),
        ("http://10.0.3.7:40050", True),
        ("http://192.168.1.5", True),
        ("http://aimeat-node:40050", True),  # a container's service name
        ("http://box.internal", True),
        ("https://koe.aimeat.io", False),
        ("https://aimeat.io", False),
        ("not a url", True),
    ],
)
def test_what_counts_as_internal(url, internal):
    assert public_url.is_internal(url) is internal


def test_the_fleets_address_comes_first(place):
    assert public_url.public_base("concierge") == PUBLIC
    assert public_url.person_link("/v1/pub/o/k", "concierge") == f"{PUBLIC}/v1/pub/o/k"
    assert (
        public_url.person_link(f"{LOOPBACK}/v1/apps/o/a.html?mode=inline", "c")
        == f"{PUBLIC}/v1/apps/o/a.html?mode=inline"
    )


def test_a_public_link_is_left_alone(place):
    assert public_url.person_link("https://example.org/a.png", "c") == "https://example.org/a.png"


def test_without_the_env_the_nodes_own_public_address_is_used(monkeypatch):
    monkeypatch.setattr(public_url, "_node_url", lambda agent: LOOPBACK)
    monkeypatch.setattr(public_url, "_node_canonical", lambda agent: "https://koe.aimeat.io")
    assert public_url.public_base("c") == "https://koe.aimeat.io"


def test_a_node_that_names_an_internal_base_is_not_taken_for_public(monkeypatch, capsys):
    monkeypatch.setattr(public_url, "_node_url", lambda agent: LOOPBACK)
    monkeypatch.setattr(public_url, "_node_canonical", lambda agent: "http://localhost:40050")
    assert public_url.public_base("c") is None
    assert public_url.person_link("/v1/pub/o/k", "c") is None
    assert public_url.person_link(f"{LOOPBACK}/v1/pub/o/k", "c") is None, "never the loopback link"
    assert "public address is unknown" in capsys.readouterr().err


def test_a_real_node_address_is_public(monkeypatch):
    monkeypatch.setattr(public_url, "_node_url", lambda agent: "https://aimeat.io")
    assert public_url.public_base("c") == "https://aimeat.io"


def test_the_crews_own_fetch_keeps_its_node_address(place):
    assert public_url.internal_url("/v1/pub/o/k", "c") == f"{LOOPBACK}/v1/pub/o/k"


# ── every person-facing builder, on a loopback place ─────────────────────────────────────────


def test_the_image_link_on_the_machine_road(place):
    from crewaimeat import seedream_gen

    link = seedream_gen._pub_url("concierge", "koeavain#x@aim-solo", "images/1.png")
    _no_loopback(link)
    assert link.startswith(PUBLIC + "/v1/pub/")


def test_the_moodboard_link(place):
    from crewaimeat import image_contract

    link = image_contract._pub_url("image-scout#x@n", "moodboards/r/01.jpg")
    _no_loopback(link)
    assert link.startswith(PUBLIC)


def test_the_map_snapshot_link(place, monkeypatch):
    from crewaimeat import aimeat_crew, map_snapshot

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"data": {"upload_url": "http://127.0.0.1:40050/upload"}}

    monkeypatch.setattr(aimeat_crew, "_aimeat_request", lambda *a, **k: R())
    monkeypatch.setattr(map_snapshot.requests, "put", lambda *a, **k: type("P", (), {"status_code": 200})())
    monkeypatch.setattr(map_snapshot, "_own_gaii", lambda agent: "mapper#x@n")
    link = map_snapshot.upload_public("mapper", "maps/1.png", b"png")
    _no_loopback(link)
    assert link.startswith(PUBLIC + "/v1/pub/")


def test_the_reader_desk_image_link(place, monkeypatch):
    from crewaimeat import reader_desk, seedream_gen, storage

    monkeypatch.setattr(reader_desk, "own_gaii", lambda agent: "desk#x@n")
    monkeypatch.setattr(reader_desk, "_token", lambda agent, owner: ("t", LOOPBACK))
    monkeypatch.setattr(reader_desk, "_discover_owner", lambda agent: "o")
    monkeypatch.setattr(storage, "fetch_bytes", lambda agent, key: (b"jpg", "image/jpeg"))
    monkeypatch.setattr(seedream_gen, "_upload_public", lambda *a, **k: True)
    [link] = reader_desk.publish_tip_images(
        "desk", [{"mime": "image/jpeg", "storage_key": "k", "name": "a.jpg"}], date="2026-10-03"
    )
    _no_loopback(link)
    assert link.startswith(PUBLIC)


def test_the_app_link(place):
    from crewaimeat.generator_tool import app_link

    link = app_link("app-builder", "owner#x", "kassa.html")
    _no_loopback(link)
    assert link == f"{PUBLIC}/v1/apps/owner#x/kassa.html?mode=inline"


def test_the_app_link_with_no_public_address_says_so_instead(monkeypatch):
    from crewaimeat.generator_tool import app_link

    monkeypatch.setattr(public_url, "_node_url", lambda agent: LOOPBACK)
    text = app_link("app-builder", "owner#x", "kassa.html")
    _no_loopback(text)
    assert "public address is unknown" in text


def test_the_approval_address_of_a_proposal(place):
    from crewaimeat.concierge_propose import public_addresses

    answer = {
        "approval_url": f"{LOOPBACK}/v1/profile?tab=agents",
        "next_step": f"Approve it at {LOOPBACK}/v1/profile?tab=agents.",
    }
    out = public_addresses(answer, "concierge")
    assert out["approval_url"] == f"{PUBLIC}/v1/profile?tab=agents"
    _no_loopback(out["next_step"])
    assert public_addresses({"approval_url": f"{PUBLIC}/x", "next_step": "ok"}, "c")["approval_url"] == f"{PUBLIC}/x"


def test_an_internal_approval_address_with_no_public_one_points_at_the_agents_page(monkeypatch):
    from crewaimeat.concierge_propose import public_addresses

    monkeypatch.setattr(public_url, "_node_url", lambda agent: LOOPBACK)
    out = public_addresses({"approval_url": f"{LOOPBACK}/p", "next_step": f"Open {LOOPBACK}/p."}, "c")
    assert out["approval_url"] is None and out["next_step"] == "Open your Agents page."


# ── the concierge's image: attached through the crew's own address, linked through the public one ──


@pytest.fixture
def concierge():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "crews" / "concierge_crew.py"
    spec = importlib.util.spec_from_file_location("concierge_links_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_concierge_attaches_through_its_own_address_and_never_shows_loopback(place, concierge, monkeypatch):
    fetched = []
    monkeypatch.setattr(
        concierge.seedream_gen,
        "generate_image",
        lambda agent, d: {"ok": True, "url": f"{PUBLIC}/v1/pub/o/k.png", "fetch_url": f"{LOOPBACK}/v1/pub/o/k.png"},
    )
    monkeypatch.setattr(concierge, "_download", lambda url, **kw: fetched.append(url) or (b"png", "image/png", "k.png"))
    monkeypatch.setattr(concierge.dm, "dm_attach_bytes", lambda *a, **k: {"id": "att"})
    sink = {"attachments": []}
    tool = next(t for t in concierge._concierge_tools(sink) if t.name == "generate_image")
    assert tool.run(description="kuva") == "Attached a generated image."
    assert fetched == [f"{LOOPBACK}/v1/pub/o/k.png"], "the bytes come through the crew's own address"


def test_when_the_attach_fails_the_concierge_gives_the_public_link(place, concierge, monkeypatch):
    monkeypatch.setattr(
        concierge.seedream_gen,
        "generate_image",
        lambda agent, d: {"ok": True, "url": f"{PUBLIC}/v1/pub/o/k.png", "fetch_url": f"{LOOPBACK}/v1/pub/o/k.png"},
    )
    monkeypatch.setattr(concierge, "_download", lambda url, **kw: None)
    tool = next(t for t in concierge._concierge_tools({"attachments": []}) if t.name == "generate_image")
    out = tool.run(description="kuva")
    _no_loopback(out)
    assert PUBLIC in out


def test_the_nodes_canonical_address_is_read_from_its_spec_link_header(monkeypatch):
    """GET /v1/spec answers `Link: <base>/v1/docs; rel="canonical"`, asked through the shared transport."""
    import importlib.util

    from crewaimeat import aimeat_crew

    # A fresh copy of the module: the conftest stubs this read on the imported one.
    spec = importlib.util.spec_from_file_location("public_url_fresh", public_url.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)

    class R:
        headers = {"Link": '<https://koe.aimeat.io/v1/docs>; rel="canonical"'}

    seen = {}
    monkeypatch.setattr(
        aimeat_crew, "_aimeat_request", lambda agent, m, path, **k: seen.update(agent=agent, path=path) or R()
    )
    assert fresh._node_canonical("concierge") == "https://koe.aimeat.io"
    assert seen == {"agent": "concierge", "path": "/v1/spec"}
