"""Embedder cascade — opt-in CrewAI crew memory picks a reachable embedder tier or fails LOUD.

Fully offline: every reachability probe and the ollama daemon check are stubbed, so no ollama / NVIDIA /
qwen call is ever real (mirrors tests/test_llm_chain.py, which stubs the LLM). Storage-path tests scope
everything under a temp AIMEAT_HOME (the tests/test_local_memory.py idiom) so nothing touches the machine.
"""

from __future__ import annotations

import pytest

from crewaimeat import embedder_cascade as ec


# --- bias / cascade ordering ------------------------------------------------
def test_privacy_bias_drops_the_free_cloud_nvidia_tier():
    # privacy keeps local ollama first, allows the paid-private qwen, and DROPS the free-but-cloud nvidia.
    assert ec._ordered_tiers("privacy") == ["ollama", "qwen"]
    # cost promotes the FREE nvidia tier ahead of paid qwen (money over privacy).
    assert ec._ordered_tiers("cost") == ["ollama", "nvidia", "qwen"]


def test_resolve_bias_defaults_and_validates(monkeypatch):
    monkeypatch.delenv("EMBEDDER_BIAS", raising=False)
    assert ec._resolve_bias(None) == "privacy"  # default = privacy
    assert ec._resolve_bias("COST") == "cost"
    assert ec._resolve_bias("garbage") == "privacy"  # unknown -> safe default
    monkeypatch.setenv("EMBEDDER_BIAS", "cost")
    assert ec._resolve_bias(None) == "cost"


# --- probe / fall-through / fail-loud (the MultiProviderLLM shape) -----------
def test_ollama_tier_uses_openai_compatible_endpoint(monkeypatch):
    monkeypatch.setattr(ec, "_tier_reachable", lambda t: (t == "ollama", "ok"))
    emb, tag = ec.resolve_embedder("a", bias="privacy")
    # ollama rides crewai's `openai` provider against .../v1 so it needs no extra `ollama` python pkg
    assert emb["provider"] == "openai"
    assert emb["config"]["api_base"].endswith("/v1")
    assert tag.startswith("ollama-")


def test_falls_through_to_next_reachable_tier(monkeypatch):
    """ollama down -> the cascade falls through to the next reachable tier (nvidia under cost bias)."""
    reach = {"ollama": (False, "daemon down"), "nvidia": (True, "ok"), "qwen": (True, "ok")}
    monkeypatch.setattr(ec, "_tier_reachable", lambda t: reach[t])
    emb, tag = ec.resolve_embedder("a", bias="cost")
    assert emb["provider"] == "openai" and "integrate.api.nvidia.com" in emb["config"]["api_base"]
    assert tag.startswith("nvidia-")


def test_no_reachable_tier_raises_loud_and_actionable(monkeypatch):
    monkeypatch.setattr(ec, "_tier_reachable", lambda t: (False, "unavailable"))
    with pytest.raises(RuntimeError, match="NO embedder is reachable"):
        ec.resolve_embedder("a", bias="cost")


def test_explicit_override_bypasses_the_cascade(monkeypatch):
    """A CrewSpec.memory_embedder override is used verbatim — no tier is even probed."""
    probed = {"n": 0}

    def _spy(_t):
        probed["n"] += 1
        return (True, "ok")

    monkeypatch.setattr(ec, "_tier_reachable", _spy)
    ov = {"provider": "openai", "config": {"model_name": "my-embed", "api_base": "http://x/v1", "api_key": "k"}}
    emb, tag = ec.resolve_embedder("a", override=ov)
    assert emb is ov and tag == "openai-my-embed" and probed["n"] == 0


# --- per-tier reachability (env-key gating + ollama daemon/model probe) ------
def test_cloud_tiers_need_their_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    ok, why = ec._tier_reachable("nvidia")
    assert not ok and "NVIDIA_API_KEY" in why
    monkeypatch.setenv("NVIDIA_API_KEY", "x")
    assert ec._tier_reachable("nvidia")[0]

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert not ec._tier_reachable("qwen")[0]
    monkeypatch.setenv("DASHSCOPE_API_KEY", "y")
    assert ec._tier_reachable("qwen")[0]


def test_ollama_reachable_requires_daemon_up_AND_model_pulled(monkeypatch):
    import crewaimeat.agency.cockpit as cockpit

    monkeypatch.setattr(cockpit, "_ollama_probe", lambda: (True, ["nomic-embed-text:latest", "gemma3:latest"]))
    assert ec._ollama_reachable("nomic-embed-text")[0]
    # daemon up but the embed model not pulled -> unavailable, with an actionable reason
    monkeypatch.setattr(cockpit, "_ollama_probe", lambda: (True, ["gemma3:latest"]))
    ok, why = ec._ollama_reachable("nomic-embed-text")
    assert not ok and "ollama pull nomic-embed-text" in why
    # daemon down -> unavailable
    monkeypatch.setattr(cockpit, "_ollama_probe", lambda: (False, []))
    assert not ec._ollama_reachable("nomic-embed-text")[0]


def test_memory_preflight_reports_without_raising(monkeypatch):
    monkeypatch.setattr(ec, "_tier_reachable", lambda t: (t == "qwen", "ok" if t == "qwen" else "down"))
    ok, why = ec.memory_preflight(bias="privacy")  # order ollama, qwen -> qwen reachable
    assert ok and "qwen" in why
    monkeypatch.setattr(ec, "_tier_reachable", lambda t: (False, "down"))
    ok, why = ec.memory_preflight()
    assert not ok and "no embedder" in why.lower()


# --- principal isolation (no wrong-caller memory) ---------------------------
def test_resolve_principal_isolates_by_caller():
    # a federation DM isolates by the sender's ghii (the cross-owner privacy boundary)
    assert ec.resolve_principal({"_source": "dm", "_dm_sender": "peer@NodeX"}) == "peer-nodex"
    # an owner-inbox message isolates by its sender
    assert (
        ec.resolve_principal({"_source": "message", "_original": {"from": "coordinator-agent"}}) == "coordinator-agent"
    )
    # a delegated/workflow task isolates by its requester
    assert ec.resolve_principal({"requestedBy": "wf-77"}) == "wf-77"
    # an ordinary owner-queued task shares the owner's own brain (never a cross-owner leak)
    assert ec.resolve_principal({"id": "t-1", "description": "do it"}) == "owner"


# --- scoped, sanitized storage under AIMEAT_HOME ----------------------------
def test_storage_path_is_scoped_and_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    p = ec.memory_store_path("News Watcher!", owner="Owner/../X", principal="peer@n", embedder_tag="ollama-nomic")
    s = p.as_posix()
    assert tmp_path.as_posix() in s and "crew_memory" in s
    assert "news-watcher" in s and "peer-n" in s and "ollama-nomic" in s
    assert ".." not in p.parts  # no path component can escape the base
    assert p.is_dir()  # created


def test_storage_scope_variants(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    principal = ec.memory_store_path("ag", owner="o", principal="p", embedder_tag="t")  # default
    assert principal.parts[-3:] == ("ag", "p", "t")
    shared = ec.memory_store_path("ag", owner="o", principal="p", embedder_tag="t", scope="agent")
    assert shared.parts[-2:] == ("_shared", "t")  # one brain across all callers
    session = ec.memory_store_path("ag", owner="o", principal="p", embedder_tag="t", scope="session", session="task-9")
    assert "task-9" in session.parts and session.parts[-1] == "t"  # ephemeral per task


def test_owner_is_the_top_isolation_segment(tmp_path, monkeypatch):
    """Two owners get physically separate subtrees — the hard privacy wall."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    a = ec.memory_store_path("ag", owner="alice", principal="owner", embedder_tag="t")
    b = ec.memory_store_path("ag", owner="bob", principal="owner", embedder_tag="t")
    assert "alice" in a.parts and "bob" in b.parts
    # neither path is a prefix of the other (no cross-owner reach)
    assert not str(a).startswith(str(b)) and not str(b).startswith(str(a))
