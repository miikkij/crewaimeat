"""JULKAISUPÖYTÄ → LÄHETIN: the approved pieces become draft posts, and a person's work is never overwritten.

The node is faked where `_aimeat_call` and `_aimeat_request` stand: owner memory holds the desk's
pieces and the gate's answer in the shape the workflow engine writes ({picks: [...]}), and the LÄHETIN
space answers reads by id the way `aimeat_workspace_read {ids}` does ({items: [{space, id, value}]}).
"""

from __future__ import annotations

import pytest

from crewaimeat import julkaisu_lahetin as jl

REF = "2026-10-16"
ORG, WS = jl.LAHETIN_ORG, jl.LAHETIN_WS
GAII = "julkaisu-toimittaja#happydude500001@aimeat-finland-001-genesis"
KUVA_URL = (
    "https://aimeat.io/v1/pub/julkaisu-kuva%23happydude500001%40aimeat-finland-001-genesis/images/20261016-ab12.png"
)

LINKEDIN = {"text": "Yhteys valmistuu nyt vasta kun olet päättänyt, mitä agentti saa tehdä.", "notes": "n"}
X_THREAD = {"text": "Post one stands alone.\n\nPost two says more.\n\nPost three ends it.", "notes": "n"}
VALINTA = {"vastaus": "valittu", "kulma": {"nro": 2, "otsikko": "Oikeudet ennen yhteyttä", "kulma": "Päätät ensin."}}
KUVAT = {"kuvat": [{"nro": 1, "url": KUVA_URL, "storage_key": "images/20261016-ab12.png", "prompt": "p"}]}
APPROVED = {"picks": ["hyvaksy"], "by": "happydude500001", "answeredAt": "2026-10-16T11:00:00Z"}


class Node:
    """Owner memory + the LÄHETIN post space + storage, with every call recorded."""

    def __init__(self, memory: dict, posts: dict | None = None):
        self.memory = dict(memory)
        self.posts = dict(posts or {})
        self.calls: list[tuple[str, dict]] = []
        self.requests: list[tuple[str, str]] = []
        self.puts: list[str] = []

    def call(self, agent, tool, payload, **kw):
        self.calls.append((tool, payload))
        if tool == "aimeat_workspace_read":
            items = [{"space": "post", "id": i, "value": self.posts[i]} for i in payload["ids"] if i in self.posts]
            return {"organism_id": payload["organism_id"], "ws": payload["ws"], "mode": "content", "items": items}
        if tool == "aimeat_workspace_write":
            self.posts[payload["id"]] = payload["value"]
            return {"ok": True, "id": payload["id"]}
        if tool == "aimeat_workspace_publish":
            return {"ok": True}
        if tool == "aimeat_memory_write":
            self.memory[payload["key"]] = payload["value"]
            return {"ok": True}
        raise AssertionError(f"unexpected tool {tool}")

    def read(self, agent, key):
        return self.memory.get(key)

    def request(self, agent, method, path, **kw):
        self.requests.append((method, path))

        class R:
            status_code = 200
            content = b"\x89PNG..."
            headers = {"Content-Type": "image/png"}

            def json(self):
                return {"ok": True, "data": {"upload_url": "https://aimeat.io/upload/once"}}

        return R()


@pytest.fixture
def node(monkeypatch):
    def make(memory: dict, posts: dict | None = None) -> Node:
        n = Node(memory, posts)
        monkeypatch.setattr(jl, "_aimeat_call", n.call)
        monkeypatch.setattr("crewaimeat.workspace_tools._aimeat_call", n.call)
        monkeypatch.setattr(jl, "read_owner_key", n.read)
        monkeypatch.setattr(jl, "_aimeat_request", n.request)
        monkeypatch.setattr(jl, "record_deliverable_key", lambda *a: None)

        class Put:
            status_code = 200

        monkeypatch.setattr("requests.put", lambda url, **kw: n.puts.append(url) or Put())
        return n

    return make


def _memory(**pieces) -> dict:
    mem = {f"julkaisu.{REF}.portti": APPROVED, f"julkaisu.{REF}.valinta": VALINTA}
    for channel, value in pieces.items():
        mem[f"julkaisu.{REF}.{channel}"] = value
    return mem


TASK = {"id": "t1", "scope": [{"name": "offer", "value": "vie-lahettimeen"}, {"name": "var.ref", "value": REF}]}


def _run(agent=GAII):
    return jl.vie_lahettimeen(agent, task=TASK, task_id="t1")


# ── the pieces become the right records ─────────────────────────────────────────────────────


def test_the_pieces_become_one_draft_post_each(node):
    n = node(_memory(linkedin=LINKEDIN, x=X_THREAD, kuvat=KUVAT))
    out = _run()
    li, x = n.posts[f"jp-{REF}-linkedin"], n.posts[f"jp-{REF}-x"]

    assert li["channels"] == ["linkedin"] and li["body"] == LINKEDIN["text"]
    assert li["title"] == "Oikeudet ennen yhteyttä", "the title is the angle the person chose"
    assert li["tags"] == ["julkaisupoyta", REF] and li["campaign"] == "Julkaisupöytä"
    assert li["plannedFor"] is None and li["published"] == []
    assert li["createdBy"] == "julkaisu-toimittaja" and li["createdAt"] and li["updatedAt"]

    assert x["channels"] == ["x"] and x["perChannel"] == {"x": {"format": "thread"}}
    assert x["body"] == "Post one stands alone.\n---\nPost two says more.\n---\nPost three ends it."

    published = [p for t, p in n.calls if t == "aimeat_workspace_publish"]
    assert {(p["namespace"], p["id"]) for p in published} == {
        ("lahetin.posts", f"jp-{REF}-linkedin"),
        ("lahetin.posts", f"jp-{REF}-x"),
    }, "each draft is published, so LÄHETIN lists it"
    writes = [p for t, p in n.calls if t == "aimeat_workspace_write"]
    assert all(p["organism_id"] == ORG and p["ws"] == WS and p["space"] == "post" for p in writes)
    assert "draft jp-2026-10-16-linkedin written" in out and "draft jp-2026-10-16-x written" in out

    memo = n.memory[f"julkaisu.{REF}.lahetin"]
    assert memo["posts"][f"jp-{REF}-x"]["updatedAt"] == x["updatedAt"], "the step remembers what it wrote"


def test_the_first_image_is_copied_into_the_space_and_attached_to_the_linkedin_post(node):
    n = node(_memory(linkedin=LINKEDIN, kuvat=KUVAT))
    _run()
    li = n.posts[f"jp-{REF}-linkedin"]["perChannel"]["linkedin"]
    assert li["storageOwner"] == GAII
    # The skill's key: lahetin/<postId>-<provider>-<file name>, and the post id already ends in -linkedin.
    assert li["storageKey"] == f"lahetin/jp-{REF}-linkedin-linkedin-20261016-ab12.png"
    get, presign = n.requests
    assert get[0] == "GET" and get[1].startswith("/v1/pub/julkaisu-kuva")
    assert presign == ("POST", "/v1/storage")
    assert n.puts == ["https://aimeat.io/upload/once"]


def test_a_picture_that_does_not_copy_leaves_the_post_without_it_and_says_so(node, monkeypatch):
    n = node(_memory(linkedin=LINKEDIN, kuvat={"kuvat": [{"nro": 1, "url": "", "storage_key": "x"}]}))
    out = _run()
    assert "storageKey" not in n.posts[f"jp-{REF}-linkedin"]["perChannel"]["linkedin"]
    assert "WITHOUT the image" in out


def test_several_versions_give_the_first_one(node):
    versions = {
        "versiot": [{"nro": 1, "kieli": "fi", "text": "Ensimmäinen."}, {"nro": 2, "kieli": "en", "text": "Second."}]
    }
    n = node(_memory(linkedin=versions))
    out = _run()
    assert n.posts[f"jp-{REF}-linkedin"]["body"] == "Ensimmäinen."
    assert "the first of 2 versions" in out


# ── what is skipped ─────────────────────────────────────────────────────────────────────────


def test_a_missing_piece_is_skipped_and_named(node):
    n = node(_memory(linkedin=LINKEDIN))
    out = _run()
    assert set(n.posts) == {f"jp-{REF}-linkedin"}
    assert f"x: no piece at julkaisu.{REF}.x" in out


@pytest.mark.parametrize("answer", [{"picks": ["hylkaa"]}, {"picks": ["uusiksi"]}, None])
def test_nothing_goes_to_lahetin_without_an_approval(node, answer):
    mem = _memory(linkedin=LINKEDIN, x=X_THREAD)
    if answer is None:
        mem.pop(f"julkaisu.{REF}.portti")
    else:
        mem[f"julkaisu.{REF}.portti"] = answer
    n = node(mem)
    out = _run()
    assert n.posts == {} and out.startswith("NOTHING WRITTEN")
    assert f"julkaisu.{REF}.lahetin" in n.memory, "the step still ends with its record, so the run sees why"


def test_a_post_a_person_edited_is_not_overwritten(node):
    n = node(_memory(linkedin=LINKEDIN, x=X_THREAD))
    _run()
    pid = f"jp-{REF}-linkedin"
    n.posts[pid] = {**n.posts[pid], "body": "Ihmisen korjaama.", "updatedAt": "2099-01-01T00:00:00Z"}
    out = _run()
    assert n.posts[pid]["body"] == "Ihmisen korjaama."
    assert f"{pid} was changed by somebody" in out
    assert f"draft jp-{REF}-x replaced" in out, "the untouched post is still the step's to replace"


def test_a_post_that_was_sent_is_not_overwritten(node):
    n = node(_memory(linkedin=LINKEDIN))
    _run()
    pid = f"jp-{REF}-linkedin"
    sent = {**n.posts[pid], "published": [{"provider": "linkedin", "attemptId": "a1", "at": "2026-10-16T12:00:00Z"}]}
    n.posts[pid] = sent
    out = _run()
    assert n.posts[pid] == sent and f"{pid} was already sent" in out


def test_a_post_this_step_did_not_write_is_left_alone(node):
    pid = f"jp-{REF}-linkedin"
    theirs = {"id": pid, "body": "Joku muu kirjoitti tämän.", "published": [], "updatedAt": "2026-10-15T00:00:00Z"}
    n = node(_memory(linkedin=LINKEDIN), posts={pid: theirs})
    out = _run()
    assert n.posts[pid] == theirs and "did not write it" in out


# ── nothing reaches the connections API ─────────────────────────────────────────────────────


def test_nothing_calls_a_connections_route(node):
    n = node(_memory(linkedin=LINKEDIN, x=X_THREAD, kuvat=KUVAT))
    _run()
    assert not any("/v1/connections" in path for _m, path in n.requests)
    assert not any("connection" in tool or "schedule" in tool for tool, _p in n.calls)


# ── where it runs ───────────────────────────────────────────────────────────────────────────


def test_the_editor_answers_its_new_offer_in_code_and_leaves_its_own_task_to_the_model(node, monkeypatch):
    import crews.julkaisu_toimittaja_crew as crew

    node(_memory(linkedin=LINKEDIN))
    assert any(o["id"] == "vie-lahettimeen" for o in crew.OFFERS)
    offer = next(o for o in crew.OFFERS if o["id"] == "vie-lahettimeen")
    assert offer["deliverable_location"] == {"key": "julkaisu.{ref}.lahetin"}
    assert len(offer["ask"]) <= 500 and len(offer["title"]) <= 120, "the node's offer schema limits"
    assert crew.on_task({"id": "t9", "scope": [{"name": "offer", "value": "valitse-aihe"}]}) is None
    assert "draft" in crew.on_task(TASK)
