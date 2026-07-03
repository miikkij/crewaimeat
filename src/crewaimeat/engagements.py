"""Contract-engagement gate for a crew's OWN poll/discovery path (audit point 5).

AIMEAT 0.14.0 gates the record-PUSH path IN THE CONNECTOR (daemon._engagement_gate): the node will not
push a workspace-record wake to an agent whose engagement for that (workspace × contract) is RETIRED.
It does NOT gate a crew's own poll/discovery loop — so a crew that scans `member_workspaces()` must run
the SAME check itself, or a Retired agent keeps working via the poll path.

This mirrors aimeat_crewai.daemon._engagement_verdict BYTE-FOR-BYTE. Mirroring notes that matter:
  * the engagement record's status field is named `state` (values "active" / "retired"), NOT `status`;
  * engagements are filtered by `agentName` (camelCase);
  * a missing `contract` compares as the empty string;
  * every read FAILS OPEN (any error -> process), so a node/tunnel hiccup never silently stops a crew.
Engagements are read FRESH on every call: a runtime Retire must take effect on the NEXT poll pass, so
they must NOT be cached (this is exactly why the connector reads them fresh per push event). Only the
stable workspace manifest (the space->contract map) is cached per process.
"""

from __future__ import annotations

import sys

# The stable workspace manifest (space->contract map) is cached per process; a failed read is NOT cached
# (so the next pass retries). Engagements are deliberately NOT cached — see the module docstring.
_SPACE_CONTRACT_CACHE: dict[tuple[str, str], dict[str, str]] = {}


def _api_get(agent: str, path: str, params: dict) -> dict | None:
    """Authed GET as `agent` (its own token) against the node. Parsed JSON on 200, else None (fail open)."""
    import requests

    from crewaimeat.generator_tool import _discover_owner, _token

    try:
        tok, url = _token(agent, _discover_owner(agent))
        r = requests.get(
            f"{url.rstrip('/')}{path}", params=params, headers={"Authorization": f"Bearer {tok}"}, timeout=10
        )
        return r.json() if r.status_code == 200 else None
    except Exception:  # noqa: BLE001 — fail open
        return None


def _agent_engagements(agent: str, org: str, ws: str) -> list[dict] | None:
    """This agent's engagements for (org, ws) — GET /v1/organisms/{org}/workspace/engagements?ws={ws},
    filtered to agentName==agent. None on a read failure (caller must fail open). Read FRESH every call
    (NOT cached) so a runtime Retire / Re-adopt takes effect on the very next poll pass."""
    data = _api_get(agent, f"/v1/organisms/{org}/workspace/engagements", {"ws": ws})
    if data is None:
        return None  # read failed -> fail open
    engs = (data.get("data") or {}).get("engagements") or []
    return [e for e in engs if isinstance(e, dict) and e.get("agentName") == agent]


def _space_contract(agent: str, org: str, ws: str, space: str) -> str | None:
    """The contract owning `space` in (org, ws), from the manifest objectType stamp {name: contract}.
    None if unknown or the read failed. Cached per (org, ws); failures are not cached."""
    key = (org, ws)
    if key not in _SPACE_CONTRACT_CACHE:
        data = _api_get(agent, f"/v1/organisms/{org}/workspace", {"ws": ws})
        if data is None:
            return None
        ots = ((data.get("data") or {}).get("manifest") or {}).get("objectTypes") or []
        _SPACE_CONTRACT_CACHE[key] = {
            ot["name"]: ot["contract"] for ot in ots if isinstance(ot, dict) and ot.get("name") and ot.get("contract")
        }
    return _SPACE_CONTRACT_CACHE.get(key, {}).get(space)


def _verdict(mine: list[dict], space_contract: str | None) -> str:
    """process | skip | backfill — byte-for-byte from daemon._engagement_verdict (§7d)."""
    if space_contract is not None:
        for e in mine:
            if (e.get("contract") or "") == space_contract:
                return "skip" if e.get("state") == "retired" else "process"
        return "backfill"
    # no space->contract mapping: decide at the workspace level
    if any(e.get("state") == "active" for e in mine):
        return "process"
    if any(e.get("state") == "retired" for e in mine):
        return "skip"
    return "process"


def should_process(agent: str, org: str, ws: str, *, contract: str | None = None, space: str | None = None) -> bool:
    """True if `agent` should process (org, ws) — same rule the 0.14.0 push gate uses. FAIL-OPEN: any read
    error -> True. Pass `contract` (the crew's own contract id) for a direct per-contract gate, or `space`
    to resolve the contract from the workspace manifest stamp; with neither it decides workspace-level."""
    mine = _agent_engagements(agent, org, ws)
    if mine is None:
        return True  # read failed -> fail open (never silently stop a crew on a hiccup)
    sc = contract if contract is not None else (_space_contract(agent, org, ws, space) if space else None)
    # "process" and "backfill" both mean: go ahead (the node owns the backfill side-effect on the push path).
    return _verdict(mine, sc) != "skip"


def engaged_pairs(agent: str, pairs: list[tuple[str, str]], *, contract: str) -> list[tuple[str, str]]:
    """Filter (org, ws) pairs to those `agent` is still engaged for `contract` — a RETIRED workspace is
    dropped (with a loud note). Use at the head of a poll/discovery loop; gate the DISCOVERY path only
    (an explicit push-event target is already gated by the connector)."""
    out: list[tuple[str, str]] = []
    for oid, wid in pairs:
        if should_process(agent, oid, wid, contract=contract):
            out.append((oid, wid))
        else:
            print(
                f"[{agent}] engagement RETIRED for contract '{contract}' in {wid} — skipping (poll gate)",
                file=sys.stderr,
            )
    return out
