"""The agent manifest — one declaration per agent, read from the crew file.

These tests pin the property the whole change exists for: **there is one place**. Forgetting to update
a central list is no longer possible because the list is derived, and the tests that matter here are
the ones that would fail if someone quietly reintroduced a second source.

Structure tests run against a synthetic crews/ under tmp_path. The last section checks the REAL tree,
but only for invariants — never for a count that goes stale the moment someone adds a crew.
"""

from __future__ import annotations

import json
from pathlib import Path

from crewaimeat import agent_manifest

PY_CREW = '''\
"""A crew."""

AGENT_NAME = "{agent}"
LLM_PROFILE = "coding"
TAGS = ["alpha", "role.task-runner"]
CAPABILITIES = {{"technical": [{{"name": "x", "type": "skill"}}], "domain": ["does a thing"], "languages": ["en"]}}
OFFERS = [{{"id": "do-a-thing", "title": "Do a thing", "ask": "Ask me; I do not do other things."}}]
SKILLS = ["some-craft"]


def build_domain(ctx):
    return ([], [])


def run() -> None:
    pass
'''


def _repo(tmp_path: Path, files: dict[str, str], defs: dict[str, dict] | None = None) -> Path:
    root = tmp_path / "repo"
    (root / "crews").mkdir(parents=True)
    for name, body in files.items():
        (root / "crews" / name).write_text(body, encoding="utf-8")
    if defs:
        (root / "crew_defs").mkdir(parents=True, exist_ok=True)
        for name, doc in defs.items():
            (root / "crew_defs" / name).write_text(json.dumps(doc), encoding="utf-8")
    return root


# ── reading a Python crew ───────────────────────────────────────────────────────────────────────
def test_a_python_crew_declares_everything_in_one_place(tmp_path):
    root = _repo(tmp_path, {"a_crew.py": PY_CREW.format(agent="a")})
    m = agent_manifest.by_agent(root, refresh=True)["a"]
    assert m.kind == "python"
    assert m.llm_profile == "coding"
    assert m.tags == ["alpha", "role.task-runner"]
    assert m.capabilities["domain"] == ["does a thing"]
    assert m.offers[0]["id"] == "do-a-thing"
    assert m.skills == ["some-craft"]


def test_reading_never_imports_the_crew(tmp_path):
    """A crew that would explode on import must still be readable. doctor runs from a pre-commit hook
    on a possibly-broken tree, and the routing resolver runs inside the fleet — neither can afford to
    execute 46 modules' worth of imports and side effects to learn six literals."""
    body = PY_CREW.format(agent="a") + "\nraise SystemExit('this module must never be executed')\n"
    root = _repo(tmp_path, {"a_crew.py": body})
    m = agent_manifest.by_agent(root, refresh=True)["a"]
    assert m.llm_profile == "coding"


def test_a_computed_declaration_is_not_a_declaration(tmp_path):
    """Only literals are read. A value assembled at runtime cannot be resolved statically, and
    pretending otherwise (an earlier draft stored a "<computed>" placeholder) silently produced an
    agent named "<computed>" — which then matched nothing and migrated nothing."""
    body = 'AGENT_NAME = "a"\nLLM_PROFILE = "cod" + "ing"\n\n\ndef build_domain(ctx):\n    return ([], [])\n\n\ndef run():\n    pass\n'
    root = _repo(tmp_path, {"a_crew.py": body})
    m = agent_manifest.by_agent(root, refresh=True)["a"]
    assert m.llm_profile is None


def test_agent_name_resolves_through_one_module_hop(tmp_path):
    """Six M-ROOM crews write `AGENT_NAME = mr.ARCHIVIST`. Read literally that is "no name", and the
    fleet then keys them by FILENAME — which is why logs/.host_status.json listed
    `mroom_archivist_crew` beside `mroom-curator`."""
    root = _repo(tmp_path, {})
    pkg = root / "src" / "crewaimeat"
    pkg.mkdir(parents=True)
    (pkg / "thing.py").write_text('ARCHIVIST = "the-archivist"\n', encoding="utf-8")
    (root / "crews" / "hop_crew.py").write_text(
        'from crewaimeat import thing as th\n\nAGENT_NAME = th.ARCHIVIST\nLLM_PROFILE = "news"\n\n\n'
        "def build_domain(ctx):\n    return ([], [])\n\n\ndef run():\n    pass\n",
        encoding="utf-8",
    )
    m = agent_manifest.by_agent(root, refresh=True)["the-archivist"]
    assert m.llm_profile == "news"


# ── reading a JSON crew ─────────────────────────────────────────────────────────────────────────
def test_a_json_crew_declares_the_same_things(tmp_path):
    """A declarative crew's loader restates nothing; the JSON doc is its declaration. Both kinds
    normalise to the same Manifest, so nothing downstream needs to know which it is reading."""
    loader = (
        "from pathlib import Path\n\n"
        'AGENT_NAME = "b"\n'
        '_DOC_PATH = Path(__file__).resolve().parent.parent / "crew_defs" / "b.json"\n\n\n'
        "def build_domain(ctx):\n    return ([], [])\n\n\ndef run():\n    pass\n"
    )
    doc = {
        "agent_name": "b",
        "llm_profile": "news",
        "tags": ["beta"],
        "capabilities": {"domain": ["writes"]},
        "offers": [{"id": "write"}],
        "skills": ["house-voice"],
    }
    root = _repo(tmp_path, {"b_crew.py": loader}, defs={"b.json": doc})
    m = agent_manifest.by_agent(root, refresh=True)["b"]
    assert m.kind == "json"
    assert (m.llm_profile, m.tags, m.skills) == ("news", ["beta"], ["house-voice"])
    assert m.offers[0]["id"] == "write"


# ── the parking rule, shared with the fleet ─────────────────────────────────────────────────────
def test_a_parked_crew_is_read_but_not_live(tmp_path):
    """Parked means the fleet skips it — not that its declaration disappears. A parked crew's routing
    and identity still belong to it, which is why they were left in the file when it was parked."""
    root = _repo(tmp_path, {"a_crew.py": PY_CREW.format(agent="a"), "_b_crew.py": PY_CREW.format(agent="b")})
    mans = agent_manifest.by_agent(root, refresh=True)
    assert agent_manifest.live_agents(root) == {"a"}
    assert mans["b"].parked and not mans["b"].live
    assert mans["b"].llm_profile == "coding"


# ── the real tree: invariants only ──────────────────────────────────────────────────────────────
def test_every_live_crew_declares_a_model_profile():
    """The silent fallback this replaced: an agent with no profile resolved to `default` with no log
    line, which put 20 of 46 crews on a profile nobody chose. A default is fine — an UNDECIDED one is
    not, so every live crew must say which it wants."""
    root = Path(__file__).resolve().parent.parent
    missing = sorted(m.agent for m in agent_manifest.all_manifests(root, refresh=True) if m.live and not m.llm_profile)
    assert not missing, f"live crews with no LLM_PROFILE: {missing}"


def test_no_two_crews_claim_the_same_agent():
    root = Path(__file__).resolve().parent.parent
    seen: dict[str, str] = {}
    for m in agent_manifest.all_manifests(root):
        if not m.agent:
            continue
        assert m.agent not in seen, f"{m.agent} is claimed by both {seen[m.agent]} and {m.path.name}"
        seen[m.agent] = m.path.name


def test_the_central_routing_map_holds_overrides_only():
    """llm_providers.json's `crews` map is now an OVERRIDE list, not the registry. An entry that
    merely repeats what the crew already declares is a second source of truth waiting to drift."""
    root = Path(__file__).resolve().parent.parent
    cfg = json.loads((root / "llm_providers.json").read_text(encoding="utf-8"))
    declared = {m.agent: m.llm_profile for m in agent_manifest.all_manifests(root) if m.agent}
    redundant = [
        agent
        for agent, profile in (cfg.get("crews") or {}).items()
        if not agent.startswith("_") and declared.get(agent) == profile
    ]
    assert not redundant, f"routing entries that only repeat the crew's own declaration: {redundant}"
