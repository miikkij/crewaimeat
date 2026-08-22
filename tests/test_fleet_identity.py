"""Fleet identity — the well-formedness floor for what every agent advertises.

This used to read `FLEET_IDENTITY`, a central dict of 45 hand-kept entries. That dict is now empty on
purpose: each crew declares its own `TAGS` / `CAPABILITIES` (a JSON crew declares `tags`/`capabilities`
in its doc) and `identity_for` resolves from there. So the floor moved with the data — it walks the
CREWS, which is the only way it can still be true of every agent rather than of a list someone
remembered to update.
"""

import re

from crewaimeat.agent_manifest import all_manifests
from crewaimeat.fleet_identity import FLEET_IDENTITY, identity_for

_TAG_RE = re.compile(r"[a-z0-9._-]+")


def _declared():
    """(agent, {tags?, capabilities?}) for every crew that declares an identity, parked included."""
    for m in all_manifests():
        if not m.agent:
            continue
        ident = {}
        if m.tags is not None:
            ident["tags"] = m.tags
        if m.capabilities is not None:
            ident["capabilities"] = m.capabilities
        if ident:
            yield m.agent, ident
    yield from FLEET_IDENTITY.items()  # the library fallback, normally empty


def test_all_tags_charset_safe():
    """AIMEAT rejects ':' and '@' in a tag. A rejected tag is not an error the crew ever sees — the
    agent simply stops being findable by it."""
    for agent, ident in _declared():
        for t in ident.get("tags", []):
            assert _TAG_RE.fullmatch(t), f"{agent}: tag {t!r} carries chars AIMEAT rejects (only [a-z0-9._-])"


def test_capabilities_wellformed():
    """`technical` entries are {name, type} OBJECTS; `domain` and `languages` are plain strings.

    The node accepts a wrong-shaped payload without complaint and the agent silently stops matching in
    discovery — which is exactly what datapkg-analyst did on every start until 2026-08-22. `run_crew`
    now rejects it at the boundary too; this catches it a commit earlier.
    """
    for agent, ident in _declared():
        caps = ident.get("capabilities")
        if caps is None:
            continue
        for tech in caps.get("technical", []):
            assert isinstance(tech, dict), f"{agent}: technical entry {tech!r} must be a {{name, type}} object"
            assert tech.get("name") and tech.get("type") in {"mcp", "skill", "tool"}, f"{agent}: bad technical {tech}"
        assert all(isinstance(d, str) and d.strip() for d in caps.get("domain", [])), f"{agent}: bad domain"
        assert all(isinstance(x, str) and x for x in caps.get("languages", [])), f"{agent}: bad languages"


def test_identity_resolves_from_the_crew_file():
    """Spot-check that the resolution actually reaches the crew, not a leftover central entry."""
    assert identity_for("tagline-translator")["capabilities"]["languages"] == ["en", "fr", "de"]
    assert "agent-builder" in identity_for("crew-forge")["tags"]
    # image-maker MAKES images; image-scout FINDS them. The distinction has to survive in the tags,
    # or the picker hands a generation request to the crew that only searches the web.
    assert "image-generation" in identity_for("image-maker")["tags"]
    assert "image-search" in identity_for("image-scout")["tags"]
    assert identity_for("totally-unknown-agent") == {}


def test_a_tags_only_crew_stays_tags_only():
    """Declaring tags without capabilities is legitimate — it means "my domain caps are already
    specific, don't overwrite them". The resolver must not invent an empty capabilities block."""
    ident = identity_for("news-fetcher")
    assert ident.get("tags")
    assert "capabilities" not in ident


def test_the_central_registry_is_empty_and_stays_that_way():
    """The point of the move. An entry here is not an error, but it IS the old shape — a list nothing
    forces you to update — so its return should be a deliberate, visible decision."""
    assert FLEET_IDENTITY == {}, (
        f"identity is declared in the crew file now; central entries reappeared for {sorted(FLEET_IDENTITY)}"
    )
