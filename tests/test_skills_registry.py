"""Skills-registry consumer (crewaimeat.skills_registry) — offline, deterministic, no network.

The node fetch is monkeypatched at the module's requests seam; token discovery at the
generator_tool seam. Covers the layout guard (traversal/escape rejection), materialization,
the loud-vs-raise failure boundary (unreachable → loud+continue; malformed content → raise),
merge precedence (local wins), and the empty→None contract (crewai rejects Agent(skills=[])).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import crewaimeat.skills_registry as reg
from crewaimeat.skills import SkillLoadError, load_skills

SKILL_MD = "---\nname: reg-skill\ndescription: what + when\n---\nRegistry-sourced expertise body."


def _entry(name: str = "reg-skill", files: dict | None = None, ref: str | None = None) -> dict:
    return {
        "ref": ref or f"user:owner/{name}",
        "name": name,
        "fileContents": files if files is not None else {"SKILL.md": SKILL_MD.replace("reg-skill", name)},
    }


def _fake_get(payload: dict | None = None, status: int = 200, exc: Exception | None = None):
    def fake(url, headers=None, timeout=None, **kw):
        if exc is not None:
            raise exc
        return SimpleNamespace(status_code=status, json=lambda: {"ok": True, "data": payload or {}})

    return fake


def _patch_auth(monkeypatch):
    from crewaimeat import generator_tool

    monkeypatch.setattr(generator_tool, "_token", lambda a, o: ("tok", "https://node.example"))
    monkeypatch.setattr(generator_tool, "_discover_owner", lambda a: "owner")


# ── layout guard: no registry-supplied path may escape the skill dir ──────────
@pytest.mark.parametrize("bad", ["../evil.md", "/abs.md", "C:/x.md", "a\\b.md", "other/x.md", ""])
def test_layout_guard_rejects(bad):
    with pytest.raises(SkillLoadError):
        reg._safe_rel_path(bad)


@pytest.mark.parametrize("ok", ["SKILL.md", "scripts/run.py", "references/deep/r.md", "assets/a.txt"])
def test_layout_guard_accepts(ok):
    assert reg._safe_rel_path(ok) == Path(ok)


# ── materialize ────────────────────────────────────────────────────────────────
def test_materialize_writes_layout(tmp_path):
    d = reg.materialize_skill(_entry(files={"SKILL.md": SKILL_MD, "references/notes.md": "notes"}), tmp_path)
    assert d == tmp_path / "reg-skill"
    assert (d / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD
    assert (d / "references" / "notes.md").read_text(encoding="utf-8") == "notes"
    (skill,) = load_skills([d])  # and it validates through the normal fail-loud path
    assert skill.name == "reg-skill"
    assert "Registry-sourced expertise body." in (skill.instructions or "")


def test_materialize_without_body_raises(tmp_path):
    with pytest.raises(SkillLoadError, match="no SKILL.md"):
        reg.materialize_skill(_entry(files={"references/only.md": "x"}), tmp_path)


def test_materialize_nameless_entry_raises(tmp_path):
    with pytest.raises(SkillLoadError):
        reg.materialize_skill({"ref": "user:o/x", "fileContents": {"SKILL.md": SKILL_MD}}, tmp_path)


# ── fetch: the loud-vs-raise boundary ─────────────────────────────────────────
def test_fetch_happy_path(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(reg.requests, "get", _fake_get({"skills": [_entry()], "unresolved": []}))
    (skill,) = reg.fetch_agent_skills("joker")
    assert skill.name == "reg-skill"
    assert skill.instructions  # activated — prompt-ready


def test_fetch_unresolved_is_loud_but_continues(monkeypatch, capsys):
    _patch_auth(monkeypatch)
    payload = {"skills": [_entry()], "unresolved": [{"ref": "node:gone", "error": "not found"}]}
    monkeypatch.setattr(reg.requests, "get", _fake_get(payload))
    skills = reg.fetch_agent_skills("joker")
    assert [s.name for s in skills] == ["reg-skill"]
    err = capsys.readouterr().err
    assert "UNRESOLVED" in err and "node:gone" in err


def test_fetch_404_is_loud_empty(monkeypatch, capsys):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(reg.requests, "get", _fake_get(status=404))
    assert reg.fetch_agent_skills("joker") == []
    assert "no skills registry" in capsys.readouterr().err


def test_fetch_transport_failure_is_loud_empty(monkeypatch, capsys):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(reg.requests, "get", _fake_get(exc=ConnectionError("node down")))
    assert reg.fetch_agent_skills("joker") == []
    assert "WITHOUT registry skills" in capsys.readouterr().err


def test_fetch_no_token_is_loud_empty(monkeypatch, capsys):
    from crewaimeat import generator_tool

    monkeypatch.setattr(generator_tool, "_token", lambda a, o: (None, None))
    monkeypatch.setattr(generator_tool, "_discover_owner", lambda a: None)
    assert reg.fetch_agent_skills("joker") == []
    assert "no token" in capsys.readouterr().err


def test_fetch_malformed_content_raises(monkeypatch):
    """A skill that FETCHES but fails validation = contract drift → raise, never warn-skip."""
    _patch_auth(monkeypatch)
    bad = _entry(files={"SKILL.md": "no frontmatter at all"})
    monkeypatch.setattr(reg.requests, "get", _fake_get({"skills": [bad], "unresolved": []}))
    with pytest.raises(SkillLoadError):
        reg.fetch_agent_skills("joker")


def test_fetch_empty_is_empty(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(reg.requests, "get", _fake_get({"skills": [], "unresolved": []}))
    assert reg.fetch_agent_skills("joker") == []


# ── merge precedence: union, LOCAL WINS, empty → None ─────────────────────────
def _mk_local(tmp_path, name: str, body: str):
    d = tmp_path / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}", encoding="utf-8")
    (skill,) = load_skills([d])
    return skill


def test_merge_union_local_wins(tmp_path, monkeypatch, capsys):
    local = _mk_local(tmp_path, "shared-name", "LOCAL body")
    registry_shared = reg.materialize_skill(
        _entry("shared-name", files={"SKILL.md": "---\nname: shared-name\ndescription: d\n---\nREGISTRY body"}),
        tmp_path / "r1",
    )
    registry_extra = reg.materialize_skill(_entry("extra-skill"), tmp_path / "r2")
    reg_skills = load_skills([registry_shared, registry_extra])

    merged = reg.merge_skills([local], reg_skills, "joker")
    assert [s.name for s in merged] == ["shared-name", "extra-skill"]
    assert "LOCAL body" in merged[0].instructions  # the local copy shadowed the registry one
    assert "earlier source wins" in capsys.readouterr().err


def test_merge_empty_is_none():
    assert reg.merge_skills(None, []) is None
    assert reg.merge_skills([], []) is None  # crewai rejects Agent(skills=[]) — None, never []


# ── 2c: ws: refs flow through the linked-skills fetch untouched ────────────────
def test_fetch_with_ws_ref_entry(monkeypatch):
    """A ws:{org}/{ws}/{name}-ref'd skill in the agent fetch is materialized like any other —
    refs are opaque to the consumer; only name + fileContents matter."""
    _patch_auth(monkeypatch)
    entry = _entry("ws-sourced-skill", ref="ws:org-123/ws-abc/ws-sourced-skill")
    monkeypatch.setattr(reg.requests, "get", _fake_get({"skills": [entry], "unresolved": []}))
    (skill,) = reg.fetch_agent_skills("joker")
    assert skill.name == "ws-sourced-skill"
    assert skill.instructions


# ── 2c: workspace target resolution (CrewSpec.workspace_skills) ────────────────
RECORDS = [
    {"organism_id": "org-1", "ws": "ws-a", "space": "shared.x"},
    {"organism_id": "org-1", "ws": "ws-a", "space": "shared.y"},  # same pair, other space
    {"organism_id": "org-1", "ws": "ws-b", "space": "shared.z"},
]


def test_workspace_targets_default_off():
    assert reg.workspace_targets(False, RECORDS) == []


def test_workspace_targets_derive_from_records_deduped():
    assert reg.workspace_targets(True, RECORDS) == [("org-1", "ws-a"), ("org-1", "ws-b")]


def test_workspace_targets_explicit_list_wins():
    explicit = [{"organism_id": "org-9", "ws": "ws-9"}]
    assert reg.workspace_targets(explicit, RECORDS) == [("org-9", "ws-9")]


def test_workspace_targets_true_without_records_is_empty():
    assert reg.workspace_targets(True, None) == []


# ── 2c: fetch_workspace_skills (list manifests → resolve each for bodies) ─────
def _ws_router(listing=None, resolves=None, list_status=200, resolve_status=200, list_exc=None):
    """URL-routing fake for the two-step workspace fetch."""
    resolves = resolves or {}

    def fake(url, params=None, headers=None, timeout=None, **kw):
        if url.endswith("/v1/skills"):
            if list_exc is not None:
                raise list_exc
            return SimpleNamespace(
                status_code=list_status, json=lambda: {"ok": True, "data": {"skills": listing or []}}
            )
        name = url.rsplit("/", 1)[-1]
        entry = resolves.get(name)
        return SimpleNamespace(
            status_code=resolve_status if entry is not None else 404,
            json=lambda: {"ok": True, "data": {"skill": entry or {}}},
        )

    return fake


def test_ws_fetch_happy_path(monkeypatch):
    _patch_auth(monkeypatch)
    entry = _entry("ws-skill", ref="ws:org-1/ws-a/ws-skill")
    monkeypatch.setattr(reg.requests, "get", _ws_router(listing=[{"name": "ws-skill"}], resolves={"ws-skill": entry}))
    (skill,) = reg.fetch_workspace_skills("joker", "owner", "org-1", "ws-a")
    assert skill.name == "ws-skill"
    assert skill.instructions


def test_ws_fetch_400_not_deployed_is_loud_empty(monkeypatch, capsys):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(reg.requests, "get", _ws_router(list_status=400))
    assert reg.fetch_workspace_skills("joker", "owner", "org-1", "ws-a") == []
    assert "2c not deployed" in capsys.readouterr().err


def test_ws_fetch_listing_failure_is_loud_empty(monkeypatch, capsys):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(reg.requests, "get", _ws_router(list_exc=ConnectionError("down")))
    assert reg.fetch_workspace_skills("joker", "owner", "org-1", "ws-a") == []
    assert "workspace skills skipped" in capsys.readouterr().err


def test_ws_fetch_single_resolve_failure_skips_loudly(monkeypatch, capsys):
    _patch_auth(monkeypatch)
    good = _entry("good-skill", ref="ws:org-1/ws-a/good-skill")
    router = _ws_router(listing=[{"name": "gone-skill"}, {"name": "good-skill"}], resolves={"good-skill": good})
    monkeypatch.setattr(reg.requests, "get", router)
    skills = reg.fetch_workspace_skills("joker", "owner", "org-1", "ws-a")
    assert [s.name for s in skills] == ["good-skill"]
    assert "resolve 'gone-skill' HTTP 404" in capsys.readouterr().err


def test_ws_fetch_malformed_content_raises(monkeypatch):
    _patch_auth(monkeypatch)
    bad = {"ref": "ws:org-1/ws-a/bad-skill", "name": "bad-skill", "fileContents": {"SKILL.md": "no frontmatter"}}
    monkeypatch.setattr(reg.requests, "get", _ws_router(listing=[{"name": "bad-skill"}], resolves={"bad-skill": bad}))
    with pytest.raises(SkillLoadError):
        reg.fetch_workspace_skills("joker", "owner", "org-1", "ws-a")


# ── 2c: build_ctx_skills — full precedence local > linked > workspace ─────────
def _skill_obj(tmp_path, name, body, sub="s"):
    d = tmp_path / sub / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}", encoding="utf-8")
    (skill,) = load_skills([d])
    return skill


def test_build_ctx_skills_precedence(monkeypatch, tmp_path):
    local = _skill_obj(tmp_path, "shared-name", "LOCAL", "l")
    linked_shadow = _skill_obj(tmp_path, "shared-name", "LINKED", "r1")
    linked_extra = _skill_obj(tmp_path, "linked-only", "LINKED", "r2")
    ws_shadow = _skill_obj(tmp_path, "linked-only", "WORKSPACE", "w1")
    ws_extra = _skill_obj(tmp_path, "ws-only", "WORKSPACE", "w2")

    monkeypatch.setattr(reg, "fetch_agent_skills", lambda a, o: [linked_shadow, linked_extra])
    monkeypatch.setattr(reg, "fetch_workspace_skills", lambda a, o, org, ws: [ws_shadow, ws_extra])

    merged = reg.build_ctx_skills("joker", "owner", [local], ws_targets=[("org-1", "ws-a")])
    by_name = {s.name: s.instructions for s in merged}
    assert by_name["shared-name"] == "LOCAL"  # local beats linked
    assert by_name["linked-only"] == "LINKED"  # linked beats workspace
    assert by_name["ws-only"] == "WORKSPACE"
    assert len(merged) == 3


def test_build_ctx_skills_no_targets_skips_ws_fetch(monkeypatch):
    monkeypatch.setattr(reg, "fetch_agent_skills", lambda a, o: [])
    monkeypatch.setattr(
        reg, "fetch_workspace_skills", lambda *a: (_ for _ in ()).throw(AssertionError("must not be called"))
    )
    assert reg.build_ctx_skills("joker", "owner", None, ws_targets=[]) is None


def test_crewspec_workspace_skills_defaults_off():
    from crewaimeat.aimeat_crew import CrewSpec

    spec = CrewSpec(agent_name="x", build_domain=lambda ctx: ([], []))
    assert spec.workspace_skills is False


# ── @semver pins: pinned refs are opaque to the consumer, like every other ref ─
def test_fetch_with_pinned_ref_entry(monkeypatch):
    """A @semver-pinned ref (user:{owner}/{name}@1.0.0) in the agent fetch materializes like
    any other — the node resolves the pin to the retained snapshot; the consumer only sees
    name + fileContents."""
    _patch_auth(monkeypatch)
    entry = _entry("pinned-skill", ref="user:owner/pinned-skill@1.0.0")
    monkeypatch.setattr(reg.requests, "get", _fake_get({"skills": [entry], "unresolved": []}))
    (skill,) = reg.fetch_agent_skills("joker")
    assert skill.name == "pinned-skill"
    assert skill.instructions


def test_unretained_pin_surfaces_as_unresolved(monkeypatch, capsys):
    """A pin older than the newest-10 retention comes back in `unresolved` — loud, no crash."""
    _patch_auth(monkeypatch)
    payload = {"skills": [], "unresolved": [{"ref": "user:owner/old-skill@0.0.1", "error": "not retained"}]}
    monkeypatch.setattr(reg.requests, "get", _fake_get(payload))
    assert reg.fetch_agent_skills("joker") == []
    err = capsys.readouterr().err
    assert "UNRESOLVED" in err and "old-skill@0.0.1" in err
