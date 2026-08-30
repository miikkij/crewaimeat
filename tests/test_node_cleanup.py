"""`crewaimeat orphans` — deleting agent records the node holds and no crew file backs.

This is the most destructive command in the repo: it ends another principal's registration and its
credentials on a live node. So what is pinned here is almost entirely about what it must NEVER take:
the owner's own interactive tool sessions, the node's hatchery, and anything a crew file still backs.

It is also the command that proves a claim I got wrong: an AGENT token cannot delete these. The node's
door is `requireRoleOrScope('owner', 'agent:delete')` AND, for an agent caller, `registeredBy` must
match — so granting a fleet agent the scope changes nothing for agents the hatchery registered. An
owner session skips that second condition entirely.

No network: the roster read and the delete are both stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewaimeat import node_cleanup

CREW = 'AGENT_NAME = "{agent}"\nLLM_PROFILE = "coding"\n\n\ndef build_domain(ctx):\n    return ([], [])\n\n\ndef run():\n    pass\n'


def _repo(tmp_path: Path, crews: list[str], served: list[str]) -> Path:
    root = tmp_path / "repo"
    (root / "crews").mkdir(parents=True)
    for a in crews:
        (root / "crews" / f"{a.replace('-', '_')}_crew.py").write_text(CREW.format(agent=a), encoding="utf-8")
    (root / ".aimeat").mkdir()
    (root / ".aimeat" / "serve.json").write_text(
        json.dumps({"agents": [{"agent": a, "owner": "o", "node_url": "https://n"} for a in served]}),
        encoding="utf-8",
    )
    return root


def _roster(monkeypatch, agents: list[dict]):
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", lambda *_a, **_k: {"agents": agents})


def _a(name: str, mode: str = "task-runner", by: str = "", seen: str = "2026-08-01T00:00:00Z") -> dict:
    return {"name": name, "mode": mode, "last_seen": seen, "created_at": seen, "registered_by": by}


# ── what it must never touch ────────────────────────────────────────────────────────────────────
def test_a_live_crew_is_never_an_orphan(tmp_path, monkeypatch):
    root = _repo(tmp_path, crews=["mine"], served=["mine"])
    _roster(monkeypatch, [_a("mine"), _a("stranger")])
    orphans, _tools = node_cleanup.find(root, "mine")
    assert [o.name for o in orphans] == ["stranger"]


def test_a_registered_agent_is_never_an_orphan_even_with_no_crew_file(tmp_path, monkeypatch):
    """serve.json holding a token for it means the fleet still talks to it. Deleting that record
    would revoke a credential something here is using."""
    root = _repo(tmp_path, crews=[], served=["registered-but-fileless"])
    _roster(monkeypatch, [_a("registered-but-fileless")])
    orphans, _tools = node_cleanup.find(root, "registered-but-fileless")
    assert orphans == []


@pytest.mark.parametrize(
    "name", ["claude-desktop-home-mcp", "goose", "vscode-claude", "openhands-prod", "chat", "scope-probe2"]
)
def test_the_owners_own_tool_sessions_are_never_offered(tmp_path, monkeypatch, name):
    """Fifteen of these are the owner at a keyboard on different clients. Deleting one would end
    their session, and it is not a fleet leftover at all."""
    root = _repo(tmp_path, crews=["mine"], served=["mine"])
    _roster(monkeypatch, [_a("mine"), _a(name, mode="interactive")])
    orphans, tools = node_cleanup.find(root, "mine")
    assert orphans == []
    assert name in tools


def test_an_interactive_agent_is_a_tool_session_whatever_it_is_called(tmp_path, monkeypatch):
    root = _repo(tmp_path, crews=["mine"], served=["mine"])
    _roster(monkeypatch, [_a("mine"), _a("something-odd", mode="interactive")])
    orphans, tools = node_cleanup.find(root, "mine")
    assert orphans == [] and tools == ["something-odd"]


def test_the_hatchery_is_held_back_from_a_sweep(tmp_path, monkeypatch, capsys):
    """The hatchery is the node feature that MAKES agents, and it is the `registeredBy` of most of the
    leftovers. Sweeping it away removes the only thing that can still account for them."""
    root = _repo(tmp_path, crews=["mine"], served=["mine"])
    _roster(monkeypatch, [_a("mine"), _a("hatchery-t-48be5aae81ef"), _a("junk", by="hatchery-t-48be5aae81ef")])
    monkeypatch.setenv("AIMEAT_OWNER_TOKEN", "owner-token")
    deleted: list[str] = []
    monkeypatch.setattr(node_cleanup, "delete", lambda name, url, tok: (deleted.append(name), (True, "deleted"))[1])
    node_cleanup.main(["--apply", "--root", str(root)])
    assert deleted == ["junk"], "the hatchery must not be swept"
    assert "holding back" in capsys.readouterr().out


# ── the owner-token gate ────────────────────────────────────────────────────────────────────────
def test_without_an_owner_token_it_lists_and_explains(tmp_path, monkeypatch, capsys):
    """The read needs no owner role, so listing must still work — and it must say precisely why the
    delete cannot, because "you need rights" sends someone to grant a scope that will not help."""
    root = _repo(tmp_path, crews=["mine"], served=["mine"])
    _roster(monkeypatch, [_a("mine"), _a("junk")])
    monkeypatch.delenv("AIMEAT_OWNER_TOKEN", raising=False)
    assert node_cleanup.main(["--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "junk" in out
    assert "AIMEAT_OWNER_TOKEN" in out and "registeredBy" in out
    assert "agent:delete" in out, "it must say the scope alone does not unlock this"


def test_apply_without_a_token_refuses(tmp_path, monkeypatch):
    root = _repo(tmp_path, crews=["mine"], served=["mine"])
    _roster(monkeypatch, [_a("mine"), _a("junk")])
    monkeypatch.delenv("AIMEAT_OWNER_TOKEN", raising=False)
    assert node_cleanup.main(["--apply", "--root", str(root)]) == 2


def test_listing_alone_changes_nothing(tmp_path, monkeypatch):
    root = _repo(tmp_path, crews=["mine"], served=["mine"])
    _roster(monkeypatch, [_a("mine"), _a("junk")])
    monkeypatch.setenv("AIMEAT_OWNER_TOKEN", "owner-token")
    called: list[str] = []
    monkeypatch.setattr(node_cleanup, "delete", lambda *a: (called.append(a[0]), (True, "x"))[1])
    node_cleanup.main(["--root", str(root)])
    assert called == [], "no --apply must mean no deletion"


# ── filtering + reporting ───────────────────────────────────────────────────────────────────────
def test_older_than_keeps_the_recently_seen(tmp_path, monkeypatch):
    root = _repo(tmp_path, crews=["mine"], served=["mine"])
    _roster(
        monkeypatch,
        [_a("mine"), _a("old", seen="2026-01-01T00:00:00Z"), _a("fresh", seen="2026-08-22T00:00:00Z")],
    )
    monkeypatch.setenv("AIMEAT_OWNER_TOKEN", "owner-token")
    deleted: list[str] = []
    monkeypatch.setattr(node_cleanup, "delete", lambda name, url, tok: (deleted.append(name), (True, "deleted"))[1])
    node_cleanup.main(["--apply", "--older-than", "30", "--root", str(root)])
    assert deleted == ["old"]


def test_a_failed_delete_is_reported_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, crews=["mine"], served=["mine"])
    _roster(monkeypatch, [_a("mine"), _a("junk")])
    monkeypatch.setenv("AIMEAT_OWNER_TOKEN", "not-an-owner-token")
    monkeypatch.setattr(node_cleanup, "delete", lambda *a: (False, "HTTP 403 ACCESS_DENIED"))
    assert node_cleanup.main(["--apply", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "403" in out
    assert "AGENT token" in out, "a 403 must point at the actual cause"


def test_an_unreadable_roster_is_an_error_not_an_empty_sweep(tmp_path, monkeypatch):
    """An empty roster read must never be taken as "there is nothing to clean" — on this command that
    would be the safe direction by luck, not by design."""
    root = _repo(tmp_path, crews=["mine"], served=["mine"])
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", lambda *_a, **_k: {})
    assert node_cleanup.main(["--root", str(root)]) == 2


# ── choosing WHICH orphans go ────────────────────────────────────────────────────────────────────
def _orphans(monkeypatch, names_ages):
    """Stand in for the node roster: [(name, age_days), …] with no crew file here."""
    from crewaimeat import node_cleanup as nc

    made = [
        nc.Orphan(
            name=n, created="2026-01-01", age_days=a, registered_by=None, mode="task-runner", last_seen="2026-08-01"
        )
        for n, a in names_ages
    ]
    monkeypatch.setattr(nc, "find", lambda root, probe: (made, []))
    monkeypatch.setattr(nc, "owner_token", lambda: None)
    return made


ROSTER = [("mroom-digger", 0), ("research-crew", 59), ("doc-fact-reader", 59), ("ledger-reader", 59)]


def test_except_spares_an_orphan_another_repo_still_backs(monkeypatch, capsys):
    """THE ONE THAT MATTERS. "No crew file HERE" is not "nobody runs it": this machine holds sibling
    checkouts and other products against the SAME owner. doc-fact-reader and ledger-reader belong to
    a live Company Brain and are 59 days cold — the SAME age as a real leftover, so `--older-than`
    cannot separate them and a sweep by age would have deleted them."""
    from crewaimeat import node_cleanup as nc

    _orphans(monkeypatch, ROSTER)
    nc.main(["--except", "doc-fact-reader,ledger-reader"])
    out = capsys.readouterr().out

    assert "2 left alone by --except: doc-fact-reader, ledger-reader" in out
    for spared in ("doc-fact-reader", "ledger-reader"):
        assert f"  {spared}  created" not in out, "a spared agent must not be listed as a target"
    assert "mroom-digger" in out and "research-crew" in out


def test_only_sweeps_nothing_else(monkeypatch, capsys):
    from crewaimeat import node_cleanup as nc

    _orphans(monkeypatch, ROSTER)
    nc.main(["--only", "mroom-digger"])
    out = capsys.readouterr().out

    assert "3 left alone by --only" in out
    assert "research-crew" in out  # named in the left-alone line, not offered for deletion


def test_a_name_that_is_not_an_orphan_is_refused(monkeypatch, capsys):
    """A typo in a delete list must not silently widen the sweep to everything else."""
    from crewaimeat import node_cleanup as nc

    _orphans(monkeypatch, ROSTER)
    rc = nc.main(["--except", "doc-fact-readr"])

    assert rc == 2
    assert "not an orphan on this node: doc-fact-readr" in capsys.readouterr().err
