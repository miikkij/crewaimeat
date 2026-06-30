"""brains — the use-time half of the brain model: a user's BRAIN = a chosen template + prose + policy,
plus the runtime that turns a brain into a live crew.

A **brain** is everything a non-developer edits to make "their agent": which `template` (author-time
skeleton, see `templates`), the `prose` ("what I want it to do, in my words"), and the `policy`
(autonomy / spend cap / model / schedule / where to publish). Switching a brain = edit prose/policy and
restart; you never touch code. Brains are versioned so the operator can roll back a tuning mistake.

Storage is the same zero-infra pattern as `session_store`/`local_memory`: one SQLite file under
AIMEAT_HOME (`brains.db`), stdlib `sqlite3`, WAL, fresh connection per call, JSON policy. The store API
is connector- and crewai-free (cheap for the cockpit to read); the runtime bits (`build_crewspec`,
`run_brain`) lazy-import the scaffold so a brain becomes an ordinary `run_crew` daemon.

    from crewaimeat import brains
    brains.save_brain("news-watcher", "topic-watcher", prose="Track Finnish AI funding…")
    brains.run_brain("news-watcher")     # apply policy, then run the crew daemon (in a crew stub)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time

from crewaimeat._home import aimeat_home

_COLUMNS = ("agent_name", "template_id", "prose", "policy", "title", "version", "created", "updated")


def _db_path() -> str:
    home = aimeat_home()
    os.makedirs(home, exist_ok=True)
    return os.path.join(home, "brains.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS brains ("
        "agent_name TEXT PRIMARY KEY, template_id TEXT NOT NULL, prose TEXT, policy TEXT, "
        "title TEXT, version INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS brain_versions ("
        "agent_name TEXT NOT NULL, version INTEGER NOT NULL, template_id TEXT, prose TEXT, policy TEXT, "
        "title TEXT, ts REAL NOT NULL, PRIMARY KEY(agent_name, version))"
    )
    return c


def _row_to_brain(row) -> dict:
    d = dict(zip(_COLUMNS, row))
    try:
        d["policy"] = json.loads(d["policy"]) if d["policy"] else {}
    except (ValueError, TypeError):
        d["policy"] = {}
    return d


def slug_agent_name(name: str) -> str:
    """Normalize to the connector's agent-id rule (v1.33+): 3-64 LOWERCASE alphanumeric + hyphens. The
    connector REJECTS anything else (e.g. an uppercase 'Mapmaker'), which silently breaks device-auth, so
    every agent name must be slugged before it becomes an identity. Returns '' if nothing usable remains."""
    import re as _re

    s = _re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower()).strip("-")
    return s[:64]


def save_brain(
    agent_name: str,
    template_id: str,
    *,
    prose: str | None = None,
    policy: dict | None = None,
    title: str | None = None,
) -> dict:
    """Create or update a brain, bumping its version and snapshotting the new version to history.

    `template_id` must be a registered template (fail loud otherwise). When `prose`/`policy` are omitted
    they fall back to the template's defaults on first create, or to the existing values on update — so
    editing just the prose keeps the policy and vice versa. Returns the saved brain.
    """
    from crewaimeat import brain_templates as templates  # local import: light registry, avoids a cycle

    tmpl = templates.get(template_id)
    if tmpl is None:
        raise ValueError(f"unknown template '{template_id}' (have: {', '.join(sorted(templates.REGISTRY))})")
    if not agent_name or not str(agent_name).strip():
        raise ValueError("agent_name is required")

    cur = get_brain(agent_name)
    now = time.time()
    prose = prose if prose is not None else (cur["prose"] if cur else tmpl.default_prose)
    if policy is None:
        policy = cur["policy"] if cur else dict(tmpl.default_policy)
    title = title if title is not None else (cur["title"] if cur else tmpl.title)
    version = (cur["version"] + 1) if cur else 1
    created = cur["created"] if cur else now
    policy_txt = json.dumps(policy or {}, ensure_ascii=False)

    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO brains(agent_name, template_id, prose, policy, title, version, created, updated) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (agent_name, template_id, prose, policy_txt, title, version, created, now),
        )
        c.execute(
            "INSERT OR REPLACE INTO brain_versions(agent_name, version, template_id, prose, policy, title, ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (agent_name, version, template_id, prose, policy_txt, title, now),
        )
    return get_brain(agent_name)  # type: ignore[return-value]


def get_brain(agent_name: str) -> dict | None:
    """The current brain for an agent, or None."""
    with _conn() as c:
        row = c.execute(f"SELECT {', '.join(_COLUMNS)} FROM brains WHERE agent_name=?", (agent_name,)).fetchone()
    return _row_to_brain(row) if row else None


def list_brains() -> list[dict]:
    """Every brain (the fleet roster the gallery/cockpit shows), newest-updated first."""
    with _conn() as c:
        rows = c.execute(f"SELECT {', '.join(_COLUMNS)} FROM brains ORDER BY updated DESC").fetchall()
    return [_row_to_brain(r) for r in rows]


def history(agent_name: str) -> list[dict]:
    """All saved versions of a brain, newest first (for the rollback picker)."""
    cols = ("agent_name", "version", "template_id", "prose", "policy", "title", "ts")
    with _conn() as c:
        rows = c.execute(
            f"SELECT {', '.join(cols)} FROM brain_versions WHERE agent_name=? ORDER BY version DESC",
            (agent_name,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        try:
            d["policy"] = json.loads(d["policy"]) if d["policy"] else {}
        except (ValueError, TypeError):
            d["policy"] = {}
        out.append(d)
    return out


def rollback(agent_name: str, version: int) -> dict:
    """Restore a past version as a NEW current version (non-destructive — history is preserved)."""
    target = next((v for v in history(agent_name) if v["version"] == version), None)
    if target is None:
        raise ValueError(f"brain '{agent_name}' has no version {version}")
    return save_brain(
        agent_name, target["template_id"], prose=target["prose"], policy=target["policy"], title=target["title"]
    )


def delete_brain(agent_name: str) -> bool:
    """Remove a brain and its history. True if one existed."""
    with _conn() as c:
        cur = c.execute("DELETE FROM brains WHERE agent_name=?", (agent_name,))
        c.execute("DELETE FROM brain_versions WHERE agent_name=?", (agent_name,))
        return cur.rowcount > 0


def rename_brain(old: str, new: str) -> bool:
    """Rename a brain in place (and its version history). True on success. No-op if `old` is missing, or
    `new` already exists / isn't a valid agent slug."""
    new = slug_agent_name(new)
    if old == new or len(new) < 3 or get_brain(old) is None or get_brain(new) is not None:
        return False
    with _conn() as c:
        c.execute("UPDATE brains SET agent_name=? WHERE agent_name=?", (new, old))
        c.execute("UPDATE brain_versions SET agent_name=? WHERE agent_name=?", (new, old))
    return True


def migrate_invalid_names() -> list[tuple[str, str]]:
    """One-time self-heal: rename any brain whose name isn't the connector's required slug (3-64 lowercase
    alphanumeric + hyphens) to its slug, so device-auth can succeed. Run at startup. Returns [(old, new)].
    An older brain created as e.g. 'Mapmaker' becomes 'mapmaker' with no action from the user."""
    fixed = []
    for b in list_brains():
        old = b["agent_name"]
        if slug_agent_name(old) != old and rename_brain(old, slug_agent_name(old)):
            fixed.append((old, slug_agent_name(old)))
    return fixed


# --------------------------------------------------------------------------------------------------
# Runtime — turn a brain into a live crew. Heavy imports are lazy so the store stays cheap to import.
# --------------------------------------------------------------------------------------------------


def apply_policy(agent_name: str) -> None:
    """Apply the brain's machine-affecting policy that lives OUTSIDE the crew object: pin (or clear) the
    per-agent model override so `get_llm` picks the operator's choice. Scheduling is applied separately
    (`ensure_brain_schedule`) because it needs the live node."""
    from crewaimeat import llm

    brain = get_brain(agent_name)
    if brain is None:
        return
    model = (brain.get("policy") or {}).get("model")
    if model:  # an llm.save_override spec ({"kind": "model"|"profile", ...})
        llm.save_override(agent_name, model)
    else:
        llm.clear_override(agent_name)


def build_crewspec(agent_name: str):
    """Build the `CrewSpec` for a brain: its template's `build` wired with the brain, under the brain's
    identity. Raises if the brain or its template is missing."""
    from crewaimeat import brain_templates as templates
    from crewaimeat.aimeat_crew import CrewSpec

    brain = get_brain(agent_name)
    if brain is None:
        raise ValueError(f"no brain for '{agent_name}'")
    tmpl = templates.get(brain["template_id"])
    if tmpl is None:
        raise ValueError(f"brain '{agent_name}' references unknown template '{brain['template_id']}'")

    def build_domain(ctx):
        return tmpl.build(ctx, brain)

    return CrewSpec(agent_name=agent_name, build_domain=build_domain)


def run_brain(agent_name: str) -> None:
    """Apply policy, then run the brain as an ordinary `run_crew` daemon. This is what a brain's crew
    stub calls; the fleet host discovers that stub and runs it like any other crew."""
    from crewaimeat.aimeat_crew import run_crew

    # Appliance web search = DuckDuckGo (zero-config, no local server). Without this, the search auto-detect
    # would grab any reachable SearXNG on localhost:21333 (e.g. a dev fleet's) and fail with its errors.
    os.environ.setdefault("WEB_SEARCH", "ddg")
    apply_policy(agent_name)
    run_crew(build_crewspec(agent_name))


def _stub_stem(agent_name: str) -> str:
    """Filesystem-safe `crews/<stem>_crew.py` stem for an agent name."""
    return re.sub(r"[^a-z0-9]+", "_", agent_name.lower()).strip("_") + "_crew"


def write_crew_stub(agent_name: str, crews_dir: str | os.PathLike | None = None) -> str:
    """Write the thin `crews/<stem>_crew.py` the fleet host discovers for this brain. The stub holds no
    behavior — it just calls `run_brain`, which reads the (editable, versioned) brain at startup. So
    switching a brain never rewrites code: edit the brain, restart the daemon. Returns the file path."""
    from crewaimeat.forge import _project_root  # the repo's canonical crews/ lives at <root>/crews

    target_dir = str(crews_dir) if crews_dir is not None else str(_project_root() / "crews")
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, f"{_stub_stem(agent_name)}.py")
    body = (
        '"""Auto-generated brain stub — do not edit. The behavior lives in the brain '
        "(crewaimeat.brains), edited in the agency cockpit; this stub only launches it.\n"
        f'Agent: {agent_name}\n"""\n\n'
        "from crewaimeat.brains import run_brain\n\n"
        f'AGENT_NAME = "{agent_name}"\n\n\n'
        "def run() -> None:\n"
        "    run_brain(AGENT_NAME)\n\n\n"
        'if __name__ == "__main__":\n'
        "    run()\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path
