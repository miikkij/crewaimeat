"""TUI phase-4 helpers: agent_meta (local llm chain + offers) and versions (installed vs latest).
agent_meta reads the repo's real llm_providers.json + offers (deterministic). versions has all its
network/subprocess probes monkeypatched."""

from crewaimeat.tui import agent_meta, render, versions


# ── agent_meta (B) — local, from llm_providers.json + offers ────────────────────
def test_model_chain_routes_news_fetcher_to_content_xai_first():
    profile, labels = agent_meta.model_chain("news-fetcher")
    assert profile == "content"
    assert labels and labels[0] == "xai:grok-4.3"  # content desk → grok first
    assert any(l.startswith("openrouter:") for l in labels)  # then the OpenRouter fallbacks


def test_model_chain_coding_profile_is_openrouter_first():
    profile, labels = agent_meta.model_chain("crew-forge")
    assert profile == "coding"
    assert labels[0].startswith("openrouter:")  # code crews → owl/gpt-oss first, not xai


def test_model_chain_unknown_agent_uses_default_profile():
    profile, labels = agent_meta.model_chain("totally-unknown-agent")
    assert profile == "content-free" and labels  # default profile, non-empty chain


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
