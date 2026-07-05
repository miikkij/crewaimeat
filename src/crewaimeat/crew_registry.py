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


def publish_crew_def(doc: dict, *, agent: str, visibility: str = "owner") -> tuple[bool, str, str]:
    """Validate ``doc`` and publish it to the AIMEAT crew registry (``crews.registry.<agent_name>``)
    using ``agent``'s token. Returns ``(ok, key, detail)``. A def that fails validation is NEVER
    published (fail loud). ``visibility``: ``owner`` (your fleet) or ``public`` (any owner can install it
    by your GAII)."""
    errors = validate_crew_doc(doc)
    if errors:
        return False, "", "INVALID crew def — not published:\n  - " + "\n  - ".join(errors)
    if visibility not in ("owner", "public"):
        return False, "", f"visibility must be 'owner' or 'public' (got {visibility!r})."
    name = doc["agent_name"]
    key = registry_key(name)
    envelope = {"version": _ENVELOPE_VERSION, "publishedAt": _now_iso(), "agent_name": name, "doc": doc}
    r = _aimeat_call(agent, "aimeat_memory_write", {"key": key, "value": envelope, "visibility": visibility})
    if r is None:
        return False, key, f"FAILED to write registry key '{key}' (no result from memory_write)."
    return True, key, f"published crew def '{name}' -> {key} (visibility={visibility})"


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
        _ok, _key, detail = publish_crew_def(doc, agent=agent_name, visibility=visibility)
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

    tools = [publish_crew, install_crew]
    for _t in tools:  # live registry I/O — never serve a cached result
        try:
            _t.cache_function = lambda *_a, **_k: False
        except Exception:  # noqa: BLE001
            print(f"[crew_registry] could not disable cache on {_t}", file=sys.stderr)
    return tools
