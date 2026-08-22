"""TUI phase-4 helpers: agent_meta (local llm chain + offers) and versions (installed vs latest).
agent_meta reads the repo's real llm_providers.json + offers (deterministic). versions has all its
network/subprocess probes monkeypatched."""

import json

import pytest

from crewaimeat.tui import agent_meta, render, versions

# A FIXTURE routing config. These three tests used to read the machine's own llm_providers.json (and
# its <AIMEAT_HOME>/llm_overrides.json), so they asserted one developer's model choices: they were
# permanently red on CI, where no such file exists, and went red again locally every time a routing
# decision changed. What the TUI actually promises is the RESOLUTION RULE — mapped crew → its profile,
# unmapped crew → the default profile, chain in declared order — and that is what is pinned here.
_CFG = {
    "default": "fallback-profile",
    "profiles": {
        "content": {
            "providers": [
                {"type": "openrouter", "models": [{"id": "a/lead"}]},
                {"type": "xai", "models": [{"id": "b/backup"}]},
            ]
        },
        "coding": {"providers": [{"type": "openrouter", "models": [{"id": "c/coder"}]}]},
        "fallback-profile": {"providers": [{"type": "ollama", "models": [{"id": "d/local"}]}]},
    },
    "crews": {"mapped-content-crew": "content", "mapped-coding-crew": "coding"},
}


@pytest.fixture
def routing(tmp_path, monkeypatch):
    """Point BOTH resolution inputs at a temp dir: the providers file and AIMEAT_HOME (which holds
    llm_overrides.json — a per-agent override on the dev machine would otherwise win over the file
    and silently change what these tests measure)."""
    cfg = tmp_path / "llm_providers.json"
    cfg.write_text(json.dumps(_CFG), encoding="utf-8")
    monkeypatch.setenv("LLM_PROVIDERS_FILE", str(cfg))
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    return cfg


# ── agent_meta (B) — local, from llm_providers.json + offers ────────────────────
def test_model_chain_uses_the_mapped_profile_in_declared_order(routing):
    profile, labels = agent_meta.model_chain("mapped-content-crew")
    assert profile == "content"
    assert labels == ["openrouter:a/lead", "xai:b/backup"]  # lead first, fallback after, order preserved


def test_model_chain_reads_a_different_profile_per_crew(routing):
    profile, labels = agent_meta.model_chain("mapped-coding-crew")
    assert profile == "coding"
    assert labels == ["openrouter:c/coder"]


def test_model_chain_unknown_agent_uses_default_profile(routing):
    """An unmapped crew resolves to the `default` profile. This is the silent fallback that put 20 of
    46 live crews on the free meta-router without a decision — pinned so the behaviour stays visible
    and any change to it is deliberate. `crewaimeat doctor` reports which crews land here."""
    profile, labels = agent_meta.model_chain("totally-unknown-agent")
    assert profile == "fallback-profile"
    assert labels == ["ollama:d/local"]


def test_model_chain_without_a_providers_file_says_so(tmp_path, monkeypatch):
    """No providers file must report the absence, never a made-up chain."""
    monkeypatch.setenv("LLM_PROVIDERS_FILE", str(tmp_path / "does-not-exist.json"))
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    profile, labels = agent_meta.model_chain("mapped-content-crew")
    assert profile == "(no llm_providers.json)" and labels == []


def test_offer_summary_returns_counts():
    from crewaimeat.offers import CREW_AGENTS

    n, wf = agent_meta.offer_summary(CREW_AGENTS[0])
    assert n >= 1 and 0 <= wf <= n
    assert agent_meta.offer_summary("not-an-offering-agent") == (0, 0)


def test_read_readme_extracts_and_strips_figlet():
    txt = agent_meta.read_readme("news-writer")
    assert txt and "Core-news desk" in txt  # the real README body
    assert "[[FIGLET" not in txt  # banner directive reduced to plain text
    assert agent_meta.read_readme("totally-unknown-agent") is None


# ── versions (A) — probes monkeypatched ─────────────────────────────────────────
def test_is_update_semantics():
    assert versions.is_update("0.4.0", "0.5.0") is True
    assert versions.is_update("0.5.0", "0.5.0") is False
    assert versions.is_update("0.5.0", None) is False
    assert versions.is_update(None, "0.5.0") is False


def test_version_report_flags_update(monkeypatch):
    monkeypatch.setattr(versions, "installed_pypi", lambda pkg="aimeat-crewai": "0.5.0")
    monkeypatch.setattr(versions, "latest_pypi", lambda pkg="aimeat-crewai": "0.6.0")
    monkeypatch.setattr(versions, "cli_version", lambda: "1.23.0")
    monkeypatch.setattr(versions, "latest_npm", lambda pkg="aimeat": "1.23.0")
    vr = versions.version_report()
    assert vr["pypi"]["update"] is True and vr["cli"]["update"] is False
    line = render.versions_line(vr)
    assert "aimeat-crewai 0.5.0" in line and "→ 0.6.0" in line and "aimeat-cli 1.23.0" in line


# ── render helpers ──────────────────────────────────────────────────────────────
def test_meta_lines_shape():
    lines = render.meta_lines("content", ["xai:grok-4.3", "openrouter:gpt-oss-120b:free"], 2, 1)
    joined = "\n".join(lines)
    assert "llm profile: content" in joined and "xai:grok-4.3" in joined
    assert "2  ([green]1[/] workflow-compatible)" in joined


def test_versions_line_placeholder_when_empty():
    assert render.versions_line({}) == "versions: …"
