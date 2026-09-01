"""AIMEAT crew registry — publish + install DECLARATIVE crew defs via AIMEAT memory.

A crew def (``crewaimeat.crew_def`` / ``forge_json``) is now a self-contained, validated JSON artifact,
so the AIMEAT analog of the direct-build pattern (author -> install via AIMEAT, no generator) is a
REGISTRY in AIMEAT memory: publish a validated def to ``crews.registry.<agent_name>`` so it is
discoverable and portable, then install it elsewhere (fetch -> RE-VALIDATE -> materialize locally via
``forge_json`` -> register + launch). It reuses the same memory primitives ``offers.py`` / ``discover``
use (``aimeat_memory_write`` / ``_read`` / ``_list`` / ``_read_public``) — no new node artifact type.

Fail loud both ways: a broken def is NEVER published, and a stored def is RE-VALIDATED before it is ever
materialized (never trust stored bytes). Public visibility lets another owner install a def by the
publisher's GAII (the cross-organism display path), same as M-ROOM's public feeds.
"""

from __future__ import annotations

import datetime
import json
import sys
from typing import Any
from zoneinfo import ZoneInfo

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.crew_def import CrewDocError, load_crew_doc, validate_crew_doc

REGISTRY_PREFIX = "crews.registry."  # crews.registry.<agent_name> — mirrors agents.<agent>.offers
_ENVELOPE_VERSION = 1


def registry_key(agent_name: str) -> str:
    """The memory key a crew def lives at in the registry."""
    return f"{REGISTRY_PREFIX}{agent_name}"


def _now_iso() -> str:
    try:
        return datetime.datetime.now(ZoneInfo("Europe/Helsinki")).isoformat()
    except Exception:  # noqa: BLE001 — tzdata missing (Windows w/o `tzdata`): UTC is a fine timestamp
        return datetime.datetime.now(datetime.timezone.utc).isoformat()


def publish_crew_def(
    doc: dict, *, agent: str, visibility: str = "owner", allow_foreign_namespace: bool = False
) -> tuple[bool, str, str]:
    """Validate ``doc`` and publish it to the AIMEAT crew registry (``crews.registry.<agent_name>``)
    using ``agent``'s token. Returns ``(ok, key, detail)``. A def that fails validation is NEVER
    published (fail loud). ``visibility``: ``owner`` (your fleet) or ``public`` (any owner can install it
    by your GAII).

    THE TOKEN DECIDES THE NAMESPACE, so ``agent`` must be the agent the definition is FOR. A memory
    write lands in the caller's own namespace, and the Crew tab reads the agent's own key — so
    publishing json-demo's definition with a sibling's token filed it under the sibling and the tab
    said "No definition yet" about a definition that existed and was in use. Nothing failed: the
    runtime found it anyway, because ``fetch_crew_def`` falls back to an ``owner_scope`` list, and
    that fallback is what hid the mistake for a day. Publishing under someone else is refused now,
    and ``allow_foreign_namespace`` is the deliberate way to say you meant it — installing a public
    definition from another owner's GAII being the case that legitimately does.
    """
    errors = validate_crew_doc(doc)
    if errors:
        return False, "", "INVALID crew def — not published:\n  - " + "\n  - ".join(errors)
    if visibility not in ("owner", "public"):
        return False, "", f"visibility must be 'owner' or 'public' (got {visibility!r})."
    name = doc["agent_name"]
    if name != agent and not allow_foreign_namespace:
        return (
            False,
            "",
            f"WRONG NAMESPACE — not published. This definition is for {name!r}, but it would be "
            f"written with {agent!r}'s token, which files it under {agent!r}. The node's Crew tab "
            f"reads {name!r}'s own key and would report no definition at all.\n"
            f"  Publish as the agent itself:  crewaimeat publish <def.json> --as {name}\n"
            f"  If {name!r} has no token yet, register it first — a definition for an agent that "
            f"cannot hold it is a definition nobody can edit.",
        )
    key = registry_key(name)
    # No `revision`. The counter is the node publish route's alone (aimeat-dev, spec v6): a second
    # counter is the same mistake as a second validator, and a CLI publish is deliberately outside
    # the numbered history — the tab says so in as many words, and the next publish there numbers on
    # from `max(.version.N) + 1`. `publishedBy` says who wrote it, which is the useful half.
    envelope = {
        "version": _ENVELOPE_VERSION,
        "publishedAt": _now_iso(),
        "agent_name": name,
        "publishedBy": agent,
        "doc": doc,
    }
    r = _aimeat_call(agent, "aimeat_memory_write", {"key": key, "value": envelope, "visibility": visibility})
    if r is None:
        return False, key, f"FAILED to write registry key '{key}' (no result from memory_write)."
    return True, key, f"published crew def '{name}' -> {key} (visibility={visibility})"


def publish_crew_def_live(doc: dict, *, agent: str) -> tuple[bool, str, str]:
    """Make ``doc`` the LIVE definition through the NODE's publish route. Returns ``(ok, key, detail)``.

    THE DIFFERENCE FROM `publish_crew_def`. That one writes the registry key directly with the agent's
    token, which works from a repo checkout and is deliberately outside the numbered history. This one
    hands the document to `aimeat_crew_publish`, and the node then does four things a memory write
    cannot: it asks the agent's OWN RUNTIME to validate (so a definition that would fail at run time is
    never written), it numbers the revision, it keeps `.version.N` restorable, and it wakes the runtime
    with `crew.def_updated` so the change is in force in seconds instead of at the next task.

    It also needs NO FILE, which is the point: an agent created by the node's basic-agents button has
    no repo on disk, so the file-reading publish path could never have served it.

    THE ONE THING IT CANNOT DO. The node refuses with AGENT_OFFLINE when the target's runtime is not
    up — MEASURED 2026-09-01 against a stopped daemon. So this cannot give a brand-new agent its FIRST
    definition: an agent with no definition cannot start (`json_agent.load_def` says so in as many
    words), and one that cannot start cannot validate its own first definition. That bootstrap belongs
    to whoever creates the agent; `seed_from_staged` keeps using the direct write for exactly that
    reason, and must not be routed here.
    """
    errors = validate_crew_doc(doc)
    if errors:
        # Locally first: the same validator the runtime would run, but with a message now rather than
        # after a round trip, and without spending the node's time on a document we know is wrong.
        return False, "", "INVALID crew def — not published:\n  - " + "\n  - ".join(errors)
    name = str(doc.get("agent_name") or "")
    key = registry_key(name)
    r = _aimeat_call(agent, "aimeat_crew_publish", {"target_agent_name": name, "doc": doc})
    if r is None:
        return (
            False,
            key,
            f"FAILED to publish '{name}' through the node. The commonest cause is that {name!r}'s "
            f"runtime is not up: the node asks it to validate the definition and answers AGENT_OFFLINE "
            f"when nobody is there. Start it and publish again.",
        )
    rev = None
    if isinstance(r, dict):
        rev = r.get("revision") or (r.get("data") or {}).get("revision")
    return True, key, f"published crew def '{name}' -> {key}" + (f" (revision {rev})" if rev else "")


def _unwrap(value: Any) -> dict | None:
    """Pull the crew doc out of a registry envelope (accepting a bare doc for forward-compat)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    doc = value.get("doc") if "doc" in value else value  # envelope {version,...,doc} OR a bare doc
    return doc if isinstance(doc, dict) else None


def fetch_crew_def(agent_name: str, *, agent: str, gaii: str | None = None) -> dict:
    """Read a crew def from the registry, RE-VALIDATE it, and return the doc. Tries own memory, then
    same-owner (``owner_scope``), then a public read by ``gaii``. Raises ``CrewDocError`` on a missing or
    invalid entry — a registry def is always re-validated before it can be materialized."""
    key = registry_key(agent_name)
    r = _aimeat_call(agent, "aimeat_memory_read", {"key": key})
    value = (r.get("value") if isinstance(r, dict) else r) if r is not None else None
    if value is None:  # a same-owner sibling may have published it (namespaced by that GAII)
        lr = _aimeat_call(agent, "aimeat_memory_list", {"owner_scope": True, "prefix": key})
        for it in ((lr or {}).get("items") if isinstance(lr, dict) else None) or []:
            if isinstance(it, dict) and it.get("key") == key and it.get("value") is not None:
                value = it["value"]
                break
    if value is None and gaii:  # a PUBLIC def published by another owner — read by their GAII
        pr = _aimeat_call(agent, "aimeat_memory_read_public", {"gaii": gaii, "key": key}, quiet=True)
        value = (pr.get("value") if isinstance(pr, dict) else pr) if pr is not None else None
    if value is None:
        raise CrewDocError([f"no crew def in the registry at '{key}'" + (f" (gaii={gaii})" if gaii else "")])
    doc = _unwrap(value)
    if doc is None:
        raise CrewDocError([f"registry entry '{key}' is not a crew-def object"])
    errors = validate_crew_doc(doc)
    if errors:
        raise CrewDocError([f"registry def '{agent_name}' failed re-validation:", *errors])
    return doc


def list_crew_defs(*, agent: str) -> list[dict]:
    """The registry entries visible to ``agent`` (own + same-owner). Returns
    ``[{agent_name, key, publishedAt, gaii}]`` — enough to show a menu and install one."""
    lr = _aimeat_call(agent, "aimeat_memory_list", {"owner_scope": True, "prefix": REGISTRY_PREFIX})
    out: list[dict] = []
    for it in ((lr or {}).get("items") if isinstance(lr, dict) else None) or []:
        key = it.get("key") if isinstance(it, dict) else None
        if not key or not key.startswith(REGISTRY_PREFIX):
            continue
        val = it.get("value")
        out.append(
            {
                "agent_name": key[len(REGISTRY_PREFIX) :],
                "key": key,
                "publishedAt": val.get("publishedAt") if isinstance(val, dict) else None,
                "gaii": it.get("owner_gaii") or it.get("gaii"),
            }
        )
    return out


def install_crew_def(name_or_doc: str | dict, *, agent: str, gaii: str | None = None, register: bool = True) -> str:
    """Materialize a crew def LOCALLY (``crew_defs/<name>.json`` + a thin loader) and, by default,
    register + launch it under the watchdog. Pass a crew-def dict, or an ``agent_name`` to FETCH it from
    the registry first (``gaii`` for a public def from another owner). ``register=False`` stops after
    materializing (the owner runs register/launch). Returns a human report. Raises ``CrewDocError`` if a
    fetched def is missing/invalid."""
    from crewaimeat import forge, forge_json

    doc = name_or_doc if isinstance(name_or_doc, dict) else fetch_crew_def(name_or_doc, agent=agent, gaii=gaii)
    ok, detail, _loader = forge_json.write_json_crew(doc)
    if not ok:
        return f"INSTALL FAILED: {detail}"
    name = doc["agent_name"]
    if not register:
        return f"INSTALLED (materialized only): {detail}\nRegister + launch it with /restart {name} (or register=True)."
    report = forge.register_and_launch(name)  # device-auth + watchdog launch (pytest-guarded)
    return f"INSTALLED '{name}': {detail}\n{report}"


def make_registry_tools(agent_name: str) -> list:
    """Registry tools for crew-forge's Fleet Operator: publish a locally-built crew def to the AIMEAT
    registry, and install one from it (materialize + register + launch). ``agent_name`` is the AIMEAT
    identity whose token does the memory I/O (crew-forge)."""
    from crewai.tools import tool

    @tool("publish_crew")
    def publish_crew(target_agent: str, visibility: str = "owner") -> str:
        """Publish an already-built crew def (crew_defs/<target_agent>.json on this machine) to the AIMEAT
        crew registry so it can be discovered and installed elsewhere. Build the crew first with
        /build-json. `visibility`: 'owner' (your own fleet) or 'public' (any owner can install it by your
        GAII). Returns the registry key it was published to, or the reason it was refused."""
        from crewaimeat.forge import _project_root
        from crewaimeat.forge_json import _doc_base

        path = _project_root() / "crew_defs" / f"{_doc_base(target_agent)}.json"
        if not path.is_file():
            return (
                f"No crew def at crew_defs/{path.name}. Build it first with /build-json, then publish '{target_agent}'."
            )
        try:
            doc = load_crew_doc(path)
        except Exception as exc:  # noqa: BLE001 — a corrupt local file is the operator's to fix, reported not raised
            return f"Could not read crew_defs/{path.name}: {exc}"
        # ONE key, TWO uses. A def published HERE is a def to SHARE: it is installed elsewhere by
        # this publisher's GAII, so the publisher's namespace is where it belongs and where
        # `install_crew --from <gaii>` looks for it. The other use — an agent's OWN definition, which
        # the node's Crew tab reads from that agent's own key — is what `publish_crew_def` refuses to
        # misfile, and is `crewaimeat publish --as <the agent itself>` on the CLI.
        _ok, _key, detail = publish_crew_def(doc, agent=agent_name, visibility=visibility, allow_foreign_namespace=True)
        return detail

    @tool("publish_crew_doc")
    def publish_crew_doc(target_agent: str, doc_json: str) -> str:
        """Make a crew definition LIVE for `target_agent` by passing the definition itself as JSON — no
        file, no repo, nothing on disk. This is how an agent gives ANOTHER agent its behaviour. The
        node validates it against that agent's own runtime first and nothing is written if it fails, so
        `target_agent` must be connected; a brand-new agent that has never started cannot be given its
        FIRST definition this way. Returns the registry key and revision, or the reason it was refused."""
        import json as _json

        try:
            doc = _json.loads(doc_json)
        except ValueError as exc:
            return f"doc_json is not valid JSON: {exc}"
        if not isinstance(doc, dict):
            return "doc_json must be a JSON object — the crew definition itself."
        doc.setdefault("agent_name", target_agent.strip())
        if doc.get("agent_name") != target_agent.strip():
            return (
                f"The definition names {doc.get('agent_name')!r} but you are publishing it for "
                f"{target_agent.strip()!r}. A definition filed under the wrong agent is one the Crew tab "
                f"never shows and the runtime never loads."
            )
        _ok, _key, detail = publish_crew_def_live(doc, agent=agent_name)
        return detail

    @tool("install_crew")
    def install_crew(target_agent: str, gaii: str = "") -> str:
        """Fetch a crew def from the AIMEAT registry and install it on THIS machine: materialize its files,
        then register + launch it under the watchdog (approve the device code in the dashboard). Pass
        `gaii` to install a PUBLIC crew def published by another owner (their GAII). Returns the install
        report, or the reason it failed (missing/invalid registry entry)."""
        try:
            return install_crew_def(target_agent, agent=agent_name, gaii=(gaii.strip() or None), register=True)
        except CrewDocError as exc:
            return f"INSTALL FAILED: {exc}"

    tools = [publish_crew, publish_crew_doc, install_crew]
    for _t in tools:  # live registry I/O — never serve a cached result
        try:
            _t.cache_function = lambda *_a, **_k: False
        except Exception:  # noqa: BLE001
            print(f"[crew_registry] could not disable cache on {_t}", file=sys.stderr)
    return tools
