"""fleet_identity registry — well-formedness floor: tags are charset-safe (the node rejects ':' and
'@'), capability entries are structurally valid, and a few key agents carry their expected identity."""

import re

from crewaimeat.fleet_identity import FLEET_IDENTITY, identity_for

_TAG_RE = re.compile(r"[a-z0-9._-]+")


def test_all_tags_charset_safe():
    for agent, ident in FLEET_IDENTITY.items():
        for t in ident.get("tags", []):
            assert _TAG_RE.fullmatch(t), f"{agent}: tag {t!r} carries chars AIMEAT rejects (only [a-z0-9._-])"


def test_capabilities_wellformed():
    for agent, ident in FLEET_IDENTITY.items():
        caps = ident.get("capabilities")
        if caps is None:
            continue
        for tech in caps.get("technical", []):
            assert tech.get("name") and tech.get("type") in {"mcp", "skill", "tool"}, f"{agent}: bad technical {tech}"
        assert all(isinstance(d, str) and d.strip() for d in caps.get("domain", [])), f"{agent}: bad domain"
        assert all(isinstance(l, str) and l for l in caps.get("languages", [])), f"{agent}: bad languages"


def test_known_agents_have_expected_identity():
    # Company Brain extractors + a creative + an infra agent — spot-check the derivation landed.
    assert "company-brain" in FLEET_IDENTITY["ledger-reader"]["tags"]
    assert any("camt" in d for d in FLEET_IDENTITY["ledger-reader"]["capabilities"]["domain"])
    assert "company-brain" in FLEET_IDENTITY["doc-fact-reader"]["tags"]
    assert FLEET_IDENTITY["tagline-translator"]["capabilities"]["languages"] == ["en", "fr", "de"]
    assert "agent-builder" in FLEET_IDENTITY["crew-forge"]["tags"]
    # domain-keep agents carry tags only (don't overwrite their already-specific domain caps)
    assert "capabilities" not in FLEET_IDENTITY["news-fetcher"]
    assert identity_for("totally-unknown-agent") == {}
