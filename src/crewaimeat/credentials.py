"""The credential this machine holds for an agent — looked up by the agent's NAME, whatever it is handed.

THE ONE PLACE A STORED CREDENTIAL IS FOUND. The connector files a credential as `<name>@<owner>`
(`tokens/<name>@<owner>.token`, `keys/<name>@<owner>.key`, `agents/<owner>/<name>/config.yaml`). An
agent whose crew is defined on the node runs under its full identity, `name#owner@node`, and every
lookup that put that string into the file name found nothing:

    No credential for always-on-check#happydude500001@aimeat-finland-001-genesis@happydude500001
    on this machine (looked for ...\\tokens\\always-on-check#happydude500001@...token and ...)

Measured 2026-10-11. Seven modules did it, each with its own two lines, and each then reported the
agent as unregistered: no linked skills, no exchange tools, no schedules, no engagements read, no
public address. The identity already SAYS the name and the owner, so nothing here needs to guess.

WHAT A STORED CREDENTIAL IS GOOD FOR. An agent enrolled with a key (every agent made since
2026-09) has no bearer on disk at all: `read_token` answers `("", node_url)` for it, and a request
built from that is `Authorization: Bearer ` with nothing after it. A node call therefore goes
through the serve daemon (`_aimeat_call`, `_aimeat_rest`, `_aimeat_request`), which holds the key
and signs. What is left for this module is what the file really knows: that the agent is connected
on this machine, who owns it, and which node it belongs to.
"""

from __future__ import annotations

import glob
import os

from crewaimeat.agent_manifest import agent_local_name, identity_owner

try:  # private helper of the package; without it there is no reader, and callers say so
    from aimeat_crewai.daemon import _read_token as _package_read_token
except Exception:  # noqa: BLE001  # pragma: no cover
    _package_read_token = None

HAS_READER = _package_read_token is not None


def read_token(agent: str, owner: str | None = None) -> tuple[str, str]:
    """`(token, node_url)` for `agent`, which may be a bare name or a full identity.

    The token is "" for an agent enrolled with a key. Raises what the package raises when this
    machine holds no credential for the agent: the connector's own words, with the paths it tried."""
    if _package_read_token is None:
        raise RuntimeError("aimeat-crewai has no credential reader in this version")
    return _package_read_token(agent_local_name(agent), owner=owner or identity_owner(agent))


def discover_owner(agent: str) -> str | None:
    """Who owns `agent` on this machine: the identity says so itself; a bare name is answered by its
    credential file (`<name>@<owner>`, a bearer or a key), and only then by AIMEAT_OWNER."""
    owner = identity_owner(agent)
    if owner:
        return owner
    from crewaimeat._home import aimeat_home

    name = glob.escape(agent_local_name(agent))
    home = aimeat_home()
    try:
        for folder, suffix in (("tokens", ".token"), ("keys", ".key")):
            for path in sorted((home / folder).glob(f"{name}@*{suffix}")):
                found = path.name[: -len(suffix)].partition("@")[2]
                if found:
                    return found
    except OSError:
        pass
    return os.environ.get("AIMEAT_OWNER")
