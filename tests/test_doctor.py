"""`crewaimeat doctor` — the reconciliation gate's own contract floor.

A quality gate without tests is an opinion, and a gate that produces false positives gets switched off
within a week. So these tests are mostly about PRECISION: each route rule is given a case that must
fire and a neighbouring case that must NOT — a dict `.get()` beside an HTTP `.get()`, a third-party
API call beside a node call, a logged handler beside a silent one.

Everything runs against a synthetic mini-repo under tmp_path. Pointing them at the real repo would
make them assert today's drift, which is exactly the mistake that left three TUI tests permanently red.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewaimeat.doctor import conformance, inventory
from crewaimeat.doctor.cli import run
from crewaimeat.doctor.model import ERROR, WARN, Finding, Report, apply_baseline, load_baseline, write_baseline

CREW = '''\
"""A crew."""

from crewaimeat.aimeat_crew import CrewSpec, run_crew

AGENT_NAME = "{agent}"


def build_domain(ctx):
    return ([], [])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain))
'''


def _repo(
    tmp_path: Path, *, crews: dict[str, str] | None = None, served: list[str] = (), routing: dict | None = None
) -> Path:
    root = tmp_path / "repo"
    (root / "crews").mkdir(parents=True)
    for fname, body in (crews or {}).items():
        (root / "crews" / fname).write_text(body, encoding="utf-8")
    (root / ".aimeat").mkdir(exist_ok=True)
    (root / ".aimeat" / "serve.json").write_text(
        json.dumps({"agents": [{"agent": a, "owner": "o", "token": "SECRET"} for a in served]}), encoding="utf-8"
    )
    if routing is not None:
        (root / "llm_providers.json").write_text(json.dumps(routing), encoding="utf-8")
    return root


# ── inventory ───────────────────────────────────────────────────────────────────────────────────
def test_parked_crews_are_not_live(tmp_path):
    root = _repo(tmp_path, crews={"a_crew.py": CREW.format(agent="a"), "_b_crew.py": CREW.format(agent="b")})
    inv = inventory.gather(root)
    assert inv.live_agents == {"a"}
    assert inv.parked_agents == {"b"}


def test_agent_name_resolves_through_one_module_hop(tmp_path, monkeypatch):
    """Six M-ROOM crews write `AGENT_NAME = mr.ARCHIVIST`. A literal-only reader sees "no agent name"
    and the fleet then keys them by FILENAME — which is why logs/.host_status.json showed
    `mroom_archivist_crew` beside `mroom-curator`. The hop must resolve."""
    root = _repo(tmp_path, crews={})
    pkg = root / "src" / "crewaimeat"
    pkg.mkdir(parents=True)
    (pkg / "thing.py").write_text('ARCHIVIST = "the-archivist"\n', encoding="utf-8")
    (root / "crews" / "hop_crew.py").write_text(
        "from crewaimeat import thing as th\n\nAGENT_NAME = th.ARCHIVIST\n\n\ndef build_domain(ctx):\n"
        "    return ([], [])\n\n\ndef run():\n    pass\n",
        encoding="utf-8",
    )
    inv = inventory.gather(root)
    assert inv.live_agents == {"the-archivist"}


def test_serve_json_tokens_are_never_read_into_the_inventory(tmp_path):
    """doctor prints its inventory; a token in it would end up in CI logs."""
    root = _repo(tmp_path, crews={"a_crew.py": CREW.format(agent="a")}, served=["a"])
    inv = inventory.gather(root)
    assert "SECRET" not in json.dumps(inv.served)
    assert "token" not in inv.served["a"]


# ── lens 1: registries ──────────────────────────────────────────────────────────────────────────
def _rules(report: Report) -> set[str]:
    return {f.rule for f in report.findings}


def test_ghost_registration_is_an_error(tmp_path):
    root = _repo(tmp_path, crews={"a_crew.py": CREW.format(agent="a")}, served=["a", "long-gone"])
    report = run(root)
    ghosts = [f for f in report.findings if f.rule == "registry.serve.ghost"]
    assert [f.subject for f in ghosts] == ["long-gone"]
    assert ghosts[0].severity == ERROR


def test_unregistered_live_crew_is_an_error(tmp_path):
    root = _repo(tmp_path, crews={"a_crew.py": CREW.format(agent="a")}, served=[])
    report = run(root)
    assert [f.subject for f in report.findings if f.rule == "registry.serve.unregistered"] == ["a"]


def test_unmapped_crew_is_reported_because_the_default_is_silent(tmp_path):
    root = _repo(
        tmp_path,
        crews={"a_crew.py": CREW.format(agent="a"), "b_crew.py": CREW.format(agent="b")},
        served=["a", "b"],
        routing={"default": "cheap", "profiles": {"cheap": {}, "good": {}}, "crews": {"a": "good"}},
    )
    report = run(root)
    assert [f.subject for f in report.findings if f.rule == "registry.routing.unmapped"] == ["b"]


def test_routing_to_an_undefined_profile_is_an_error(tmp_path):
    root = _repo(
        tmp_path,
        crews={"a_crew.py": CREW.format(agent="a")},
        served=["a"],
        routing={"default": "cheap", "profiles": {"cheap": {}}, "crews": {"a": "nonexistent"}},
    )
    report = run(root)
    bad = [f for f in report.findings if f.rule == "registry.routing.unknown_profile"]
    assert bad and bad[0].severity == ERROR


def test_a_crew_without_run_is_dead_weight(tmp_path):
    root = _repo(tmp_path, crews={"a_crew.py": "AGENT_NAME = 'a'\n\n\ndef build_domain(ctx):\n    return ([], [])\n"})
    report = run(root)
    assert "crew.run.missing" in _rules(report)


# ── lens 2: route conformance ───────────────────────────────────────────────────────────────────
def _scan(tmp_path: Path, rel: str, body: str) -> Report:
    root = tmp_path / "repo"
    (root / "crews").mkdir(parents=True, exist_ok=True)
    (root / "src" / "crewaimeat").mkdir(parents=True, exist_ok=True)
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    report = Report()
    conformance.check(root, report)
    return report


def test_direct_node_http_is_flagged(tmp_path):
    report = _scan(
        tmp_path,
        "src/crewaimeat/thing.py",
        'import requests\n\n\ndef go(base, tok):\n    return requests.get(f"{base}/v1/memory/x", '
        'headers={"Authorization": tok})\n',
    )
    assert "route.node.direct_http" in _rules(report)


def test_third_party_http_is_not_flagged(tmp_path):
    """The rule is about the NODE, not about using requests. A search API or a feed must stay silent,
    or the whole rule becomes noise and gets ignored."""
    report = _scan(
        tmp_path,
        "src/crewaimeat/thing.py",
        'import requests\n\n\ndef go():\n    return requests.get("https://searx.example/search?q=x")\n',
    )
    assert "route.node.direct_http" not in _rules(report)


def test_a_dict_get_is_not_an_http_call(tmp_path):
    """The precision case. A naive rule matching `.get(` flags every dict lookup in the repo — the
    first draft of this check produced 95 'findings', of which about 30 were real."""
    report = _scan(
        tmp_path,
        "src/crewaimeat/thing.py",
        'import requests\n\n\ndef go(data):\n    url = data.get("node_url")\n    '
        'return (data.get("/v1/x") or {}).get("y")\n',
    )
    assert "route.node.direct_http" not in _rules(report)


def test_the_dispatcher_itself_is_allowed_to_do_http(tmp_path):
    report = _scan(
        tmp_path,
        "src/crewaimeat/aimeat_crew.py",
        'import requests\n\n\ndef _aimeat_rest(base):\n    return requests.get(f"{base}/v1/memory/x")\n',
    )
    assert "route.node.direct_http" not in _rules(report)


def test_a_crew_building_its_own_llm_is_an_error(tmp_path):
    report = _scan(
        tmp_path,
        "crews/x_crew.py",
        'from crewai import LLM\n\nAGENT_NAME = "x"\n\n\ndef build_domain(ctx):\n    '
        'llm = LLM(model="openrouter/whatever")\n    return ([], [])\n',
    )
    hits = [f for f in report.findings if f.rule == "route.llm.direct"]
    assert hits and hits[0].severity == ERROR


def test_a_crew_using_ctx_llm_is_fine(tmp_path):
    report = _scan(
        tmp_path,
        "crews/x_crew.py",
        'AGENT_NAME = "x"\n\n\ndef build_domain(ctx):\n    return ([ctx.llm], [])\n',
    )
    assert "route.llm.direct" not in _rules(report)


def test_a_silently_swallowed_publish_is_flagged(tmp_path):
    report = _scan(
        tmp_path,
        "src/crewaimeat/thing.py",
        "def go(publish, out):\n    try:\n        publish(out)\n    except Exception:\n        pass\n",
    )
    hits = [f for f in report.findings if f.rule == "guard.silent_sink"]
    assert hits and hits[0].severity == WARN


def test_a_logged_failure_is_not_flagged(tmp_path):
    """Handling the error is fine. DISCARDING it is not. The rule must distinguish the two, or it
    just tells people to stop using try/except."""
    report = _scan(
        tmp_path,
        "src/crewaimeat/thing.py",
        "def go(publish, out):\n    try:\n        publish(out)\n    except Exception as exc:\n"
        '        print(f"publish failed: {exc}")\n',
    )
    assert "guard.silent_sink" not in _rules(report)


def test_a_swallowed_non_sink_is_not_flagged(tmp_path):
    """A discarded diagnostic probe is a judgement call, not a defect. Only WRITES someone waits for."""
    report = _scan(
        tmp_path,
        "src/crewaimeat/thing.py",
        "def go(measure):\n    try:\n        measure()\n    except Exception:\n        pass\n",
    )
    assert "guard.silent_sink" not in _rules(report)


def test_a_second_connector_version_literal_is_an_error(tmp_path):
    report = _scan(tmp_path, "src/crewaimeat/thing.py", 'CMD = "npx aimeat@2.0.0 connect"\n')
    hits = [f for f in report.findings if f.rule == "guard.version_literal"]
    assert hits and hits[0].severity == ERROR


def test_a_version_inside_a_comment_is_history_not_a_source(tmp_path):
    report = _scan(tmp_path, "src/crewaimeat/thing.py", "X = 1  # bumped from aimeat@2.0.0 in August\n")
    assert "guard.version_literal" not in _rules(report)


def test_an_unparsable_file_is_reported_not_skipped(tmp_path):
    """A file doctor cannot read is the one most likely to be broken. Silently skipping it would make
    a broken tree look clean — the exact false green this whole tool exists to prevent."""
    report = _scan(tmp_path, "src/crewaimeat/broken.py", "def (:\n")
    assert "conformance.unparsable" in _rules(report)


def test_a_profile_note_that_contradicts_its_own_order_is_flagged(tmp_path):
    """Two profiles claimed in prose that the free meta-router LED while their arrays already led with
    the paid model — for weeks. These notes are the only record of WHY a model leads, so people trust
    them instead of reading the array; a note that contradicts its data is worse than no note."""
    root = _repo(
        tmp_path,
        crews={"a_crew.py": CREW.format(agent="a")},
        served=["a"],
        routing={
            "default": "p",
            "crews": {"a": "p"},
            "profiles": {
                "p": {
                    "_note": "free/router LEADS; paid/model is only the fallback",
                    "providers": [
                        {"type": "openrouter", "models": [{"id": "paid/model"}]},
                        {"type": "openrouter", "models": [{"id": "free/router"}]},
                    ],
                }
            },
        },
    )
    report = run(root)
    assert "registry.routing.note_contradicts_order" in _rules(report)


def test_a_profile_note_matching_its_order_is_silent(tmp_path):
    root = _repo(
        tmp_path,
        crews={"a_crew.py": CREW.format(agent="a")},
        served=["a"],
        routing={
            "default": "p",
            "crews": {"a": "p"},
            "profiles": {
                "p": {
                    "_note": "paid/model LEADS; free/router is the fallback",
                    "providers": [
                        {"type": "openrouter", "models": [{"id": "paid/model"}]},
                        {"type": "openrouter", "models": [{"id": "free/router"}]},
                    ],
                }
            },
        },
    )
    report = run(root)
    assert "registry.routing.note_contradicts_order" not in _rules(report)


def test_a_skill_named_through_a_module_constant_counts_as_used(tmp_path):
    """`skill_body(EDITORIAL_SKILL)` is the normal shape once a name is used twice. A literal-only
    scanner calls that skill unused while it is driving the newspaper's editorial voice — a false
    report of exactly the kind that teaches people to ignore a check."""
    root = _repo(tmp_path, crews={"a_crew.py": CREW.format(agent="a")}, served=["a"])
    (root / "skills" / "house-voice").mkdir(parents=True)
    (root / "skills" / "house-voice" / "SKILL.md").write_text("---\nname: house-voice\n---\nbody\n", encoding="utf-8")
    pkg = root / "src" / "crewaimeat"
    pkg.mkdir(parents=True)
    (pkg / "pipe.py").write_text(
        'EDITORIAL_SKILL = "house-voice"\n\n\ndef go():\n    return skill_body(EDITORIAL_SKILL)\n', encoding="utf-8"
    )
    report = run(root)
    assert "registry.skill.missing" not in _rules(report)
    assert not any("loaded by nothing" in n for n in report.notes)


def test_a_contract_agent_is_not_accused_of_having_no_offer(tmp_path, monkeypatch):
    """Five agents advertise through a workspace CONTRACT (`_OFFER_META`) instead of an authored
    `OFFERS` list — web-researcher advertises three that way. Flagging them as "declares no OFFERS" is
    a false accusation, and a check that cries wolf is a check people switch off."""
    root = _repo(tmp_path, crews={"a_crew.py": CREW.format(agent="a")}, served=["a"])
    monkeypatch.setattr("crewaimeat.doctor.registries._contract_offer_agents", lambda: {"a"})
    report = run(root)
    assert "registry.offer.missing" not in _rules(report)


def test_a_skill_that_does_not_exist_is_an_error(tmp_path):
    root = _repo(tmp_path, crews={"a_crew.py": CREW.format(agent="a")}, served=["a"])
    (root / "crews" / "b_crew.py").write_text(
        'AGENT_NAME = "b"\n\n\ndef build_domain(ctx):\n    return ([], [])\n\n\n'
        'def run():\n    run_crew(CrewSpec(agent_name=AGENT_NAME, skills=["typo-skill"]))\n',
        encoding="utf-8",
    )
    report = run(root)
    hits = [f for f in report.findings if f.rule == "registry.skill.missing"]
    assert hits and hits[0].severity == ERROR


# ── the baseline ratchet ────────────────────────────────────────────────────────────────────────
def test_baseline_silences_recorded_findings_but_not_new_ones(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    old = Finding("some.rule", ERROR, "old-thing", "m")
    new = Finding("some.rule", ERROR, "new-thing", "m")
    write_baseline(root, [old])
    report = Report(findings=[old, new])
    enforced, stale = apply_baseline(report, load_baseline(root))
    assert [f.subject for f in enforced.findings] == ["new-thing"]
    assert stale == []


def test_a_baseline_entry_that_no_longer_fires_is_reported_as_stale(tmp_path):
    """The baseline may only shrink. Without this it silently becomes a permanent amnesty for
    problems that were fixed months ago."""
    root = tmp_path / "repo"
    root.mkdir()
    write_baseline(root, [Finding("some.rule", ERROR, "fixed-thing", "m")])
    enforced, stale = apply_baseline(Report(findings=[]), load_baseline(root))
    assert enforced.findings == []
    assert stale == ["some.rule::fixed-thing"]


def test_a_stale_baseline_entry_alone_still_reads_as_FAIL(tmp_path, capsys):
    """The verdict line must agree with the exit code. A stale entry fails under --strict, but the
    renderer counted only errors and warnings — so the gate printed "PASS" directly above a non-zero
    exit, which is how people learn to stop trusting a gate."""
    from crewaimeat.doctor.cli import _render

    text = _render(Report(findings=[]), ["some.rule::fixed-thing"], strict=True, colour=False)
    assert "FAIL" in text and "PASS" not in text
    assert "stale baseline" in text


def test_an_unreadable_baseline_stops_the_run(tmp_path):
    """A corrupt baseline must never be read as 'nothing accepted' — that silently re-enables every
    finding and buries the real problem in noise."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "doctor-baseline.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        load_baseline(root)


# ── the real repo: one guarantee, no drift assertions ───────────────────────────────────────────
def test_doctor_runs_clean_enough_on_this_repo_to_be_trusted():
    """The offline lenses must complete on the REAL tree without crashing, and the baseline must
    account for everything that is left. This asserts the GATE works — never a specific count, which
    would go stale the moment someone fixes something."""
    root = Path(__file__).resolve().parent.parent
    raw = run(root)
    enforced, _stale = apply_baseline(raw, load_baseline(root))
    assert "registries" in raw.lenses_run and "conformance" in raw.lenses_run
    assert not enforced.errors, "unbaselined doctor errors: " + ", ".join(f.key for f in enforced.errors[:10])
