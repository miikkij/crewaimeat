"""The two lists agency 2.0 keeps on this machine: the AIMEAT instances, and the agents that run HERE.

What an agent IS (its crew definition) is not stored here — it lives on the node. This only remembers
which instance an agent belongs to and that this machine runs it, which the node cannot know.

ONE NAME PER MACHINE. One connector home serves every instance, and the runtime is started with the
agent's BARE name (a GAII breaks the staged-definition lookup, spec §7 V0 finding 1). So an agent name
is unique across all instances here, and `add_agent` refuses a duplicate instead of letting two
instances' agents collide in the daemon.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from crewaimeat.agency2 import paths

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")  # the connector's rule: 3-64 lowercase + hyphens


class StoreError(ValueError):
    """A refusal a person can act on — the message is shown as is."""


def normalize_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if u and "://" not in u:
        u = "https://" + u
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise StoreError(f"not an address: {url!r}")
    return f"{p.scheme}://{p.netloc}"


def check_agent_name(name: str) -> str:
    n = (name or "").strip()
    if not _NAME_RE.match(n):
        raise StoreError(
            "the name must be 3-64 characters: lowercase letters, digits and hyphens, "
            "starting and ending with a letter or digit"
        )
    return n


# ── instances ────────────────────────────────────────────────────────────────


def _instances_path():
    return paths.state_dir() / "instances.json"


def instances() -> list[dict]:
    return paths.read_json(_instances_path(), [])


def instance(url: str) -> dict | None:
    u = normalize_url(url)
    return next((i for i in instances() if i["url"] == u), None)


def add_instance(url: str, owner: str, *, node_id: str | None = None, label: str | None = None) -> dict:
    u = normalize_url(url)
    owner = (owner or "").strip()
    if not owner:
        raise StoreError("the AIMEAT user name is required")
    rows = [i for i in instances() if i["url"] != u]
    row = {"url": u, "owner": owner, "node_id": node_id, "label": label or urlparse(u).netloc, "added": time.time()}
    rows.append(row)
    paths.write_json(_instances_path(), rows)
    return row


def remove_instance(url: str) -> None:
    u = normalize_url(url)
    if any(a["instance"] == u for a in agents()):
        raise StoreError("this instance still has agents on this machine — remove them first")
    paths.write_json(_instances_path(), [i for i in instances() if i["url"] != u])


# ── agents that run on this machine ─────────────────────────────────────────


def _agents_path():
    return paths.state_dir() / "agents.json"


def agents() -> list[dict]:
    return paths.read_json(_agents_path(), [])


def agent(name: str) -> dict | None:
    return next((a for a in agents() if a["name"] == name), None)


def known(name: str) -> str | None:
    """The name AS STORED in this app's own list, or None. Everything downstream — a subprocess
    argument, a file name — uses this value, never the string a request carried."""
    for a in agents():
        if a["name"] == name:
            return a["name"]
    return None


def add_agent(name: str, instance_url: str, *, description: str = "") -> dict:
    n = check_agent_name(name)
    inst = instance(instance_url)
    if inst is None:
        raise StoreError("add the instance first")
    existing = agent(n)
    if existing and existing["instance"] != inst["url"]:
        raise StoreError(f"'{n}' already runs here for {existing['instance']} — one name per machine, pick another")
    rows = [a for a in agents() if a["name"] != n]
    row = {
        "name": n,
        "instance": inst["url"],
        "owner": inst["owner"],
        "description": description,
        "added": (existing or {}).get("added") or time.time(),
        "connected": (existing or {}).get("connected", False),
        "autostart": (existing or {}).get("autostart", True),
    }
    rows.append(row)
    paths.write_json(_agents_path(), rows)
    return row


def update_agent(name: str, **fields) -> dict:
    rows = agents()
    for a in rows:
        if a["name"] == name:
            a.update(fields)
            paths.write_json(_agents_path(), rows)
            return a
    raise StoreError(f"no agent '{name}' on this machine")


def remove_agent(name: str) -> None:
    paths.write_json(_agents_path(), [a for a in agents() if a["name"] != name])
