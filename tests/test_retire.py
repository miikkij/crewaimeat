"""`crewaimeat retire` — the command that stops an agent participating.

It touches LIVE state: it renames a crew file, edits serve.json (which holds every agent's token) and
moves token files. So the tests here are mostly about the things that must never go wrong on a command
like that — the plan changes nothing, the backup exists before the edit, another agent's registration
is untouched, and memory is never in scope.

They also exist because retire had a real crash: `_registry_reminder` read `Inventory.identity`, which
the doctor refactor had renamed to `fallback_identity`. Nothing imported retire in a test, so the only
way to find it was to run it on the live fleet — mid-retire, after serve.json had already been edited.
"""

from __future__ import annotations

import json
from pathlib import Path

from crewaimeat import retire

CREW = 'AGENT_NAME = "{agent}"\nLLM_PROFILE = "coding"\n\n\ndef build_domain(ctx):\n    return ([], [])\n\n\ndef run():\n    pass\n'


def _repo(tmp_path: Path, monkeypatch, crews: dict[str, str], served: list[str]) -> Path:
    root = tmp_path / "repo"
    (root / "crews").mkdir(parents=True)
    for name, body in crews.items():
        (root / "crews" / name).write_text(body, encoding="utf-8")
    home = root / ".aimeat"
    (home / "tokens").mkdir(parents=True)
    (home / "serve.json").write_text(
        json.dumps({"agents": [{"agent": a, "owner": "o", "token": f"TOKEN-{a}"} for a in served]}), encoding="utf-8"
    )
    for a in served:
        (home / "tokens" / f"{a}@o.token").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("AIMEAT_HOME", str(home))
    monkeypatch.chdir(root)
    return root


def test_the_plan_changes_nothing(tmp_path, monkeypatch):
    """`retire <agent>` without --apply must be a pure read. Anything else makes the preview a trap."""
    root = _repo(tmp_path, monkeypatch, {"a_crew.py": CREW.format(agent="a")}, served=["a"])
    before = {p.name: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    steps = retire.plan(root, "a", purge_node=False)
    after = {p.name: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after
    assert any("park" in s.what for s in steps)


def test_apply_parks_the_crew_and_drops_only_that_registration(tmp_path, monkeypatch):
    root = _repo(
        tmp_path,
        monkeypatch,
        {"a_crew.py": CREW.format(agent="a"), "keep_crew.py": CREW.format(agent="keep")},
        served=["a", "keep"],
    )
    steps = retire.apply(root, "a", purge_node=False)
    assert all(s.done for s in steps), [s for s in steps if not s.done]

    assert not (root / "crews" / "a_crew.py").exists()
    assert (root / "crews" / "_a_crew.py").exists(), "the crew file is PARKED, never deleted"
    assert (root / "crews" / "keep_crew.py").exists()

    agents = json.loads((root / ".aimeat" / "serve.json").read_text(encoding="utf-8"))["agents"]
    assert [x["agent"] for x in agents] == ["keep"]
    assert agents[0]["token"] == "TOKEN-keep", "the other agent's token must survive the edit"


def test_serve_json_is_backed_up_before_it_is_edited(tmp_path, monkeypatch):
    """serve.json holds every agent's token. A bad edit costs a full re-auth of the whole fleet, so
    the backup is not optional politeness — it is the difference between a mistake and an outage."""
    root = _repo(tmp_path, monkeypatch, {"a_crew.py": CREW.format(agent="a")}, served=["a", "keep"])
    retire.apply(root, "a", purge_node=False)
    backups = list((root / ".aimeat").glob("*.before-retire-a"))
    assert backups, "no backup written"
    restored = json.loads(backups[0].read_text(encoding="utf-8"))
    assert {x["agent"] for x in restored["agents"]} == {"a", "keep"}, "the backup must be the PRE-edit file"


def test_the_token_is_stashed_not_deleted(tmp_path, monkeypatch):
    """Re-registering after a mistaken retire should be cheap. Deleting the token makes it a device-auth
    round trip with the owner at the dashboard."""
    root = _repo(tmp_path, monkeypatch, {"a_crew.py": CREW.format(agent="a")}, served=["a"])
    retire.apply(root, "a", purge_node=False)
    assert not (root / ".aimeat" / "tokens" / "a@o.token").exists()
    assert (root / ".aimeat" / "tokens" / "retired" / "a@o.token").exists()


def test_routing_override_is_removed_but_other_crews_keep_theirs(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch, {"a_crew.py": CREW.format(agent="a")}, served=["a"])
    (root / "llm_providers.json").write_text(json.dumps({"crews": {"a": "coding", "b": "news"}}), encoding="utf-8")
    retire.apply(root, "a", purge_node=False)
    crews = json.loads((root / "llm_providers.json").read_text(encoding="utf-8"))["crews"]
    assert crews == {"b": "news"}


def test_retiring_a_ghost_with_no_crew_file_still_works(tmp_path, monkeypatch):
    """The commonest case: the code was deleted long ago and only the registration is left."""
    root = _repo(tmp_path, monkeypatch, {"a_crew.py": CREW.format(agent="a")}, served=["a", "ghost"])
    steps = retire.apply(root, "ghost", purge_node=False)
    assert all(s.done for s in steps)
    assert [
        x["agent"] for x in json.loads((root / ".aimeat" / "serve.json").read_text(encoding="utf-8"))["agents"]
    ] == ["a"]


def test_retire_never_names_memory_as_something_it_touches(tmp_path, monkeypatch):
    """A retired agent's deliverables are still the owner's data. If this ever starts deleting memory,
    it should have to change this test to say so."""
    root = _repo(tmp_path, monkeypatch, {"a_crew.py": CREW.format(agent="a")}, served=["a"])
    plan_text = " ".join(s.what for s in retire.plan(root, "a", purge_node=False))
    assert "memory never touched" in plan_text
    for s in retire.apply(root, "a", purge_node=False):
        assert "memor" not in f"{s.what} {s.detail}".lower() or "never" in s.detail.lower()


def test_apply_is_idempotent(tmp_path, monkeypatch):
    """Running it twice must not fail — a half-finished retire (which is exactly what a crash leaves)
    has to be completable by running the same command again."""
    root = _repo(tmp_path, monkeypatch, {"a_crew.py": CREW.format(agent="a")}, served=["a"])
    retire.apply(root, "a", purge_node=False)
    steps = retire.apply(root, "a", purge_node=False)
    assert all(s.done for s in steps), [s for s in steps if not s.done]
