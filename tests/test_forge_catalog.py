"""crew-forge capability catalog (Slice 1): preflight gating, forge-owned tool bindings, and a
dry-run that writes a generated crew using the new _tools(ctx) mechanism and validates it in the
subprocess validator — WITHOUT ever registering or launching an agent (no live-fleet mutation).

All deterministic. The one subprocess test executes build_domain, which constructs the web/memory/
schedule tools; none of those do a blocking network call at construction (web caches a fast probe
then falls back to keyless DDG; schedule/memory are pure closures / a filesystem glob).
"""

from __future__ import annotations

from crewaimeat import forge, forge_catalog
from crewaimeat._validate_crew import _is_toollike


# ── preflight: env-missing capabilities are hidden from the Architect ──────────
def test_env_free_caps_always_available():
    avail = {c.id for c in forge_catalog.available_capabilities()}
    assert {"web", "memory", "schedule", "delegate", "app_build"} <= avail


def test_image_gated_on_openrouter_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert "image" not in {c.id for c in forge_catalog.available_capabilities()}
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-used")
    assert "image" in {c.id for c in forge_catalog.available_capabilities()}


# ── resolve: unknown / unavailable ids are DROPPED (never silently attached) ───
def test_resolve_splits_usable_and_dropped(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-used")
    usable, dropped = forge_catalog.resolve("web, image, bogus_tool")
    assert usable == ["web", "image"]
    assert dropped == ["bogus_tool"]


def test_resolve_drops_env_unavailable(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    usable, dropped = forge_catalog.resolve("web, image")
    assert usable == ["web"]
    assert dropped == ["image"]


def test_parse_ids_is_forgiving():
    assert forge_catalog.parse_ids('web, Schedule\n "memory"') == ["web", "schedule", "memory"]
    assert forge_catalog.parse_ids("") == []
    assert forge_catalog.parse_ids(None) == []


# ── forge OWNS the tricky bindings: tuple-unpack + name-filter, not the LLM ─────
def test_emit_tools_function_compiles_and_has_keys():
    src, usable, dropped = forge_catalog.emit_tools_function("web, memory, schedule")
    assert usable == ["web", "memory", "schedule"] and dropped == []
    compile(src, "<emitted>", "exec")  # valid Python
    assert src.startswith("def _tools(ctx):")
    for key in ("web", "memory", "schedule"):
        assert f'"{key}":' in src


def test_emit_owns_tuple_unpack_and_name_filter():
    src, _u, _d = forge_catalog.emit_tools_function("app_build, delegate")
    compile(src, "<emitted>", "exec")
    # the (tools, state) unpack and the delegation-subset filter are emitted by forge, not the Architect
    assert "_author_tools, _author_state = make_author_tools(AGENT_NAME, task_id=tid)" in src
    assert 'getattr(t, "name", "") in ("discover_crews", "delegate_and_wait")' in src


def test_emit_empty_when_nothing_usable():
    assert forge_catalog.emit_tools_function("")[0] == ""
    assert forge_catalog.emit_tools_function("bogus_only")[0] == ""


def test_required_scopes_and_owner_actions():
    assert forge_catalog.required_scopes(["schedule", "web"]) == ["schedule"]
    assert forge_catalog.required_scopes(["app_build"]) == ["generator"]
    assert forge_catalog.owner_actions(["delegate"])  # non-empty: the shared Data-Access tag note


def test_render_brief_lists_tools_and_the_reference_idiom():
    brief = forge_catalog.render_catalog_brief()
    assert "T = _tools(ctx)" in brief
    for cap_id in ("web", "schedule"):
        assert f"- {cap_id}:" in brief


# ── the generated FILE: with capabilities it emits _tools; without, it stays legacy ──
def test_written_file_embeds_tools_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    build_domain = (
        "def build_domain(ctx):\n"
        "    T = _tools(ctx)\n"
        '    a = Agent(role="R", goal="G", backstory="B", llm=ctx.llm, tools=[*T["web"]])\n'
        '    t = Task(description="do it", expected_output="x", agent=a)\n'
        "    return [a], [t]\n"
    )
    dest = forge.write_crew_file("cat-demo", build_domain, capabilities="web, memory")
    text = dest.read_text(encoding="utf-8")
    assert "def _tools(ctx):" in text
    assert '"web": _web_tools()' in text
    # when capabilities are wired, the legacy top-level _web_tools import is not emitted
    assert "from crewaimeat.crew import _web_tools  # web search" not in text


def test_written_file_legacy_when_no_capabilities(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    build_domain = "def build_domain(ctx):\n    return [Agent(role='r', goal='g', backstory='b', llm=ctx.llm)], [Task(description='d', expected_output='o')]\n"
    dest = forge.write_crew_file("legacy-demo", build_domain)
    text = dest.read_text(encoding="utf-8")
    assert "def _tools(ctx):" not in text
    assert "from crewaimeat.crew import _web_tools" in text


# ── DRY RUN: a generated crew with real tools passes the subprocess validator ──
def test_generated_crew_with_tools_validates(tmp_path, monkeypatch):
    """End-to-end for Slice 1: emit + write + validate. Never registers/launches (no _aimeat calls)."""
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    build_domain = (
        "def build_domain(ctx):\n"
        "    T = _tools(ctx)\n"
        "    a = Agent(\n"
        '        role="Researcher", goal="find things", backstory="you research",\n'
        '        llm=ctx.llm, tools=[*T["web"], *T["memory"], *T["schedule"]],\n'
        "    )\n"
        '    t = Task(description="do the thing", expected_output="a result", agent=a)\n'
        "    return [a], [t]\n"
    )
    dest = forge.write_crew_file("cat-runner", build_domain, capabilities="web, memory, schedule", subdir=".candidates")
    ok, detail = forge.validate_crew_file(dest)
    assert ok, f"validator rejected the generated crew: {detail}"


# ── Slice 2: preflight taxonomy (env + deps), validator tool-check, dry-run ────
def test_dep_preflight_gates(monkeypatch):
    real = forge_catalog.Capability(id="x", purpose="p", when_to_use="w", imports=(), expr="[]", deps=("json",))
    bogus = forge_catalog.Capability(
        id="y", purpose="p", when_to_use="w", imports=(), expr="[]", deps=("no_such_pkg_zzz",)
    )
    assert forge_catalog.preflight(real)[0] is True
    ok, reason = forge_catalog.preflight(bogus)
    assert ok is False and "no_such_pkg_zzz" in reason


def test_is_toollike_flags_containers_and_unpacked_tuples():
    class _FakeTool:
        name = "web_search"

    assert _is_toollike(_FakeTool()) is True
    # the un-unpacked (tools, state) tuple and other raw containers are NOT tools
    for bad in [([], {}), {"a": 1}, ["x"], "web", object()]:
        assert _is_toollike(bad) is False


def test_registration_checklist_surfaces_scopes_and_owner_setup():
    assert forge_catalog.registration_checklist("web") == ""  # nothing extra needed
    sched = forge_catalog.registration_checklist("schedule")
    assert "schedule" in sched and "grant" in sched.lower()
    deleg = forge_catalog.registration_checklist("delegate")
    assert "Owner setup" in deleg


def test_capabilities_in_source_reads_back_wiring():
    src, _u, _d = forge_catalog.emit_tools_function("web, schedule")
    assert set(forge_catalog.capabilities_in_source(src)) == {"web", "schedule"}
    assert forge_catalog.capabilities_in_source("def build_domain(ctx):\n    return [],[]") == []


def test_dry_run_build_validates_without_registering(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    build_domain = (
        "def build_domain(ctx):\n"
        "    T = _tools(ctx)\n"
        '    a = Agent(role="R", goal="G", backstory="B", llm=ctx.llm, tools=[*T["web"]])\n'
        '    return [a], [Task(description="d", expected_output="o", agent=a)]\n'
    )
    ok, detail, path = forge.dry_run_build("dryrun-demo", build_domain, capabilities="web")
    assert ok, detail
    assert path.parent.name == ".candidates"  # staged, not in the live crews/ dir


# ── the launch guard: an invalid crew must NEVER be registered or launched ─────
def test_register_and_launch_refuses_invalid_crew(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    (tmp_path / "crews").mkdir()
    # build_domain returns an empty task list → the validator rejects it (a stand-in for the real
    # eval failures where a broken crew was still shipped to launch).
    (tmp_path / "crews" / "bad_agent_crew.py").write_text(
        "from crewai import Agent, Task\n"
        "from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew\n"
        "AGENT_NAME = 'bad-agent'\n"
        "def build_domain(ctx):\n"
        "    a = Agent(role='R', goal='G', backstory='B', llm=ctx.llm)\n"
        "    return [a], []\n",
        encoding="utf-8",
    )
    calls: list = []
    monkeypatch.setattr(forge, "register_agent", lambda *a, **k: calls.append("register") or (True, "x"))
    monkeypatch.setattr(forge, "launch_crew", lambda *a, **k: calls.append("launch") or (1, "log"))
    out = forge.register_and_launch("bad-agent")
    assert "REFUSED" in out
    assert calls == []  # neither registered nor launched


def test_register_and_launch_proceeds_on_valid_crew(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    bd = (
        "def build_domain(ctx):\n"
        "    a = Agent(role='R', goal='G', backstory='B', llm=ctx.llm)\n"
        "    return [a], [Task(description='d', expected_output='o', agent=a)]\n"
    )
    forge.write_crew_file("good-agent", bd)
    launched: list = []
    monkeypatch.setattr(forge, "is_crew_running", lambda name: False)
    monkeypatch.setattr(forge, "launch_crew", lambda rel: launched.append(rel) or (999, "log/path"))
    out = forge.register_and_launch("good-agent")
    assert "NEW AGENT" in out and launched  # validated, then launched


# ── Slice 3: identity (tags/capabilities) + discover emission ──────────────────
def test_derive_identity_builds_valid_tags_and_capabilities():
    # DOMAIN is the kebab-token format the Architect is asked to emit.
    tags, caps = forge_catalog.derive_identity("web, schedule", "competitive-intelligence market-research")
    assert tags[0] == "role.task-runner"
    assert {"web-search", "scheduling", "competitive-intelligence", "market-research"} <= set(tags)
    skills = {t["name"] for t in caps["technical"]}
    assert {"web-research", "scheduling"} <= skills
    assert all(t["type"] == "skill" for t in caps["technical"])  # node drops the report on any other type
    assert caps["domain"] == ["competitive-intelligence", "market-research"]
    assert caps["languages"] == ["en"]


def test_sanitize_tag_coerces_charset():
    assert forge_catalog._sanitize_tag("Consumes:Feedback@1") == "consumes-feedback-1"
    assert forge_catalog._sanitize_tag("  Market Research ") == "market-research"


def test_written_file_emits_identity_and_discover(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    bd = (
        "def build_domain(ctx):\n"
        "    T = _tools(ctx)\n"
        '    a = Agent(role="R", goal="G", backstory="B", llm=ctx.llm, tools=[*T["web"]])\n'
        '    return [a], [Task(description=ctx.prompt or "", expected_output="o", agent=a)]\n'
    )
    dest = forge.write_crew_file("id-demo", bd, capabilities="web", domain="market-research", discover=True)
    text = dest.read_text(encoding="utf-8")
    assert "_TAGS = [" in text and "_CAPABILITIES = {" in text
    assert "discover=True" in text and "tags=_TAGS" in text and "capabilities=_CAPABILITIES" in text
    ok, detail = forge.validate_crew_file(dest)
    assert ok, detail  # identity constants + kwargs still produce a valid crew


def test_no_identity_block_when_no_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    bd = (
        "def build_domain(ctx):\n"
        "    a = Agent(role='R', goal='G', backstory='B', llm=ctx.llm)\n"
        "    return [a], [Task(description='d', expected_output='o', agent=a)]\n"
    )
    dest = forge.write_crew_file("plain-demo", bd)  # no caps, no domain, no discover
    text = dest.read_text(encoding="utf-8")
    assert "_TAGS" not in text and "discover=True" not in text


# ── Slice 3: inline offer emission ─────────────────────────────────────────────
def test_build_offer_meta_fills_enums_and_appends_negative_scope():
    m = forge_catalog.build_offer_meta(
        "release-notes-writer", "Send me a changelog and I return polished notes", "changelog -> notes"
    )
    assert m["id"] == "release-notes-writer" and m["title"] == "Release Notes Writer"
    assert m["cost"] in ("free", "cheap", "expensive") and m["latency"] in ("seconds", "minutes", "long-running")
    assert m["repeatability"] == "idempotent" and m["verification"] == "ungated"
    assert m["consequences"] == [] and m["sample"] is None
    # the ask had no negative-scope WORD ("notes" must not count as "not") -> a clause is appended
    assert forge_catalog._has_negative_scope(m["ask"])


def test_build_offer_meta_keeps_authored_negative_scope():
    m = forge_catalog.build_offer_meta("x", "I summarize sources; I do NOT invent facts", "e")
    assert m["ask"] == "I summarize sources; I do NOT invent facts"  # already has negative scope, untouched


def test_build_offer_meta_none_when_no_ask():
    assert forge_catalog.build_offer_meta("x", "") is None
    assert forge_catalog.build_offer_meta("x", "   ") is None


def test_has_negative_scope_is_word_based():
    assert not forge_catalog._has_negative_scope("I return notes and another summary")  # 'notes' != 'not'
    assert forge_catalog._has_negative_scope("I do not guess")
    assert forge_catalog._has_negative_scope("no opinions, don't predict")


def test_written_file_emits_offer_and_validates(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    bd = (
        "def build_domain(ctx):\n"
        "    a = Agent(role='R', goal='G', backstory='B', llm=ctx.llm)\n"
        "    return [a], [Task(description=ctx.prompt or 'x', expected_output='o', agent=a)]\n"
    )
    meta = forge_catalog.build_offer_meta("svc-agent", "Send X, get Y; does NOT do Z", "an example")
    dest = forge.write_crew_file("svc-agent", bd, offer=meta)
    text = dest.read_text(encoding="utf-8")
    assert "_OFFER = {" in text and "offer=_OFFER" in text
    ok, detail = forge.validate_crew_file(dest)
    assert ok, detail
