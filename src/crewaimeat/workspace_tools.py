"""Workspace tools — READ the owner's organisms and workspaces, by name, from inside a crew.

Why this exists. Asked for "an agent that gathers the CRM's open deals every morning", the concierge of a
freshly bought place recommended CrewAI Studio and named HubSpot, Salesforce and Pipedrive (2026-10-02).
The person's own CRM, CADENCE, is a workspace in their organism, and nothing the concierge held could see
it. These tools are how a crew sees what the person keeps: the organisms they belong to, each one's
workspaces by name, and one workspace's index (its spaces and record titles) or the records themselves.

Two consumers, one implementation:
  - the concierge's `look_at_my_workspaces`, so a proposal names the person's own data;
  - the `workspace` tool id in crew_def's TOOL_REGISTRY, so the agent it proposes reads that data the same
    way. Without it a JSON-defined agent could read a workspace record only if handed its exact memory key,
    which the proposer would have to reconstruct -- a guess written into somebody else's agent.

Reading and writing are two tool ids, because they are two trust decisions. `workspace` only reads.
`workspace_write` (make_workspace_write_tools) also writes, and an agent gets it only when its definition
names it -- which is what the owner reads and approves. The Yrittajan peruspaketti CRM agent (aimeat-apps
bundles/BUNDLE-AGENTS.md) is the first one: it adds and updates contacts, deals and tasks in CADENCE.

A workspace holds two kinds of space, and the writing tool reaches both:
  - RECORD spaces (CADENCE's crm.contacts, crm.deals, crm.tasks): one record per id. The node writes a
    DRAFT and something has to PUBLISH it before an app shows it, and a write REPLACES the whole record.
    Both are traps for a model, so they are code here: `write_record` publishes in the same call, and an
    update is merged onto the record as it stands, so a model that sends only the changed field does not
    wipe the rest of the contact.
  - ROW spaces (crm.mail): appended, never published. The same row id again replaces that row, which is
    how a row is updated.

Access is the node's, and it is two different gates. LISTING an organism's workspaces is gated by
membership (routes/organisms/workspace-access.ts), and the connector merges the owner's own organisms in,
so a same-owner agent sees them. READING one -- its index or its records -- also needs the scope
`organism:read` (routes/organisms/workspace-read.ts). An agent without it lists CADENCE and reads nothing
in it, which is what the live test measured before the proposal learned to ask for the scope.
WRITING a record takes `memory:write` (the draft is a memory write) and `organism:write` (the publish,
routes/organisms/gates.ts); appending rows takes `organism:write` (routes/organisms/workspace-rows.ts).
WRITE_SCOPES names them for whoever proposes an agent with this tool. Nothing here checks them first: the
node does, and its refusal is handed to the model in its own words.
Everything goes through `_aimeat_call`, the connector's /local/call surface every crew uses.
"""

from __future__ import annotations

import json
import uuid

from crewaimeat.aimeat_crew import _aimeat_call

# What the node asks of each tool id (see the module docstring for where each one is enforced).
READ_SCOPES = ("organism:read",)
WRITE_SCOPES = ("organism:read", "memory:write", "organism:write")


class WorkspaceWriteError(RuntimeError):
    """A write the node did not accept, or could not be asked about. The message is the node's own."""


def _items(data, key: str) -> list[dict]:
    """The list under `key` in a tool's answer, or the answer itself when it already is the list."""
    got = data.get(key) if isinstance(data, dict) else data
    return [x for x in got if isinstance(x, dict)] if isinstance(got, list) else []


def list_workspaces(agent_name: str) -> list[dict]:
    """Every workspace this agent can see, as {organism_id, organism, ws, name}.

    One organism the agent cannot read does not hide the others: a probe of an organism it is not an
    active member of is an expected answer while scanning, so it is asked quietly and skipped.
    """
    orgs = _items(_aimeat_call(agent_name, "aimeat_organism_list", {}) or {}, "organisms")
    out: list[dict] = []
    for o in orgs:
        oid = o.get("id")
        if not oid:
            continue
        wl = _aimeat_call(agent_name, "aimeat_workspace_list", {"organism_id": oid}, quiet=True) or {}
        for w in _items(wl, "workspaces"):
            if w.get("id"):
                out.append(
                    {
                        "organism_id": oid,
                        "organism": o.get("name") or oid,
                        "ws": w["id"],
                        "name": w.get("name") or w["id"],
                    }
                )
    return out


def find_workspace(found: list[dict], name: str) -> dict | None:
    """The workspace whose name (or id) matches `name`, ignoring case; an exact match beats a partial one."""
    want = (name or "").strip().lower()
    if not want:
        return None
    exact = [w for w in found if want in (w["name"].lower(), w["ws"].lower())]
    if exact:
        return exact[0]
    partial = [w for w in found if want in w["name"].lower()]
    return partial[0] if len(partial) == 1 else None


def resolve_workspace(agent_name: str, workspace: str, organism: str = "") -> tuple[dict | None, str]:
    """`(the workspace, "")`, or `(None, what to say instead)`, for a workspace NAMED by the model.

    THE MODEL NAMES; CODE LOOKS UP. The tools used to take raw organism and workspace ids, and a proposed
    agent with no ids in its prompt guessed them -- "default", "owner", "aimeat" -- instead of listing what
    its owner has (hosted place, 2026-10-02). Now the model says which workspace in words (its name or
    id, and the organism's name or id when two organisms have one of that name), the owner's real list is
    read here, and a miss answers with that list, so the next step is a choice from it rather than
    another guess.
    """
    found = list_workspaces(agent_name)
    org = (organism or "").strip().lower()
    if org:
        found = [w for w in found if org in (w["organism_id"].lower(), str(w["organism"]).lower())]
    want = (workspace or "").strip().lower()
    exact = [w for w in found if want and want in (w["name"].lower(), w["ws"].lower())]
    if len(exact) > 1:
        return None, (
            f"More than one workspace is called '{workspace}'. Say which organism it is in.\n"
            + render_workspaces(exact)
        )
    hit = exact[0] if exact else find_workspace(found, workspace)
    if hit is None:
        return None, f"There is no workspace '{workspace}' here. {render_workspaces(list_workspaces(agent_name))}"
    return hit, ""


def workspace_index(agent_name: str, organism_id: str, ws: str):
    """The workspace's index: its spaces and, per space, each record's id and title. No bodies.

    None when the node answered nothing. The index is the node's own small form of a workspace however
    large it is (aimeat_workspace_read without ids), so it is safe to hand to a model whole.
    """
    return _aimeat_call(agent_name, "aimeat_workspace_read", {"organism_id": organism_id, "ws": ws}, quiet=True)


def workspace_records(agent_name: str, organism_id: str, ws: str, ids: list[str]):
    """The full values of the records `ids` in one workspace."""
    return _aimeat_call(
        agent_name, "aimeat_workspace_read", {"organism_id": organism_id, "ws": ws, "ids": ids}, quiet=True
    )


def render_workspaces(found: list[dict]) -> str:
    if not found:
        return "No organisms or workspaces are visible to me."
    lines = ["Organisms and workspaces I can see:"]
    for w in found:
        lines.append(f"- {w['name']} (workspace {w['ws']}) in organism {w['organism']} ({w['organism_id']})")
    return "\n".join(lines)


def make_workspace_tools(agent_name: str) -> list:
    """The crew_def `workspace` tool: list the owner's workspaces, read one's index or its records."""
    from crewai.tools import tool

    @tool("list_workspaces")
    def list_workspaces_tool() -> str:
        """List the organisms and workspaces your owner keeps, by name. Call this FIRST when you do not
        know the workspace's exact name; never guess one."""
        return render_workspaces(list_workspaces(agent_name))

    @tool("read_workspace")
    def read_workspace(workspace: str, ids: str = "", organism: str = "") -> str:
        """Read a workspace, named as list_workspaces shows it (e.g. "CADENCE"). Without `ids`: its index
        (the spaces and every record's id and title). With `ids` (comma-separated record ids from the
        index): those records in full. `organism` only when two organisms have a workspace of that name."""
        hit, why = resolve_workspace(agent_name, workspace, organism)
        if hit is None:
            return why
        wanted = [i.strip() for i in (ids or "").split(",") if i.strip()]
        data = (
            workspace_records(agent_name, hit["organism_id"], hit["ws"], wanted)
            if wanted
            else workspace_index(agent_name, hit["organism_id"], hit["ws"])
        )
        if data is None:
            return f"Could not read the workspace {hit['name']} (not readable for me, or it is empty)."
        return json.dumps(data, ensure_ascii=False)

    return [list_workspaces_tool, read_workspace]


def _refused(answer, what: str) -> str | None:
    """The node's own words when `answer` is not a success, else None."""
    if answer is None:
        return f"The node did not answer the {what}; nothing is known to have been written."
    if isinstance(answer, dict) and answer.get("ok") is False:
        err = answer.get("error") or {}
        return f"The node refused the {what} ({err.get('code', 'refused')}): {err.get('message', '')}".rstrip(": ")
    return None


def _object_type(index, space: str) -> dict | None:
    """The manifest's object type named `space` (by its name or its namespace)."""
    manifest = index.get("manifest") if isinstance(index, dict) else None
    for ot in (manifest or {}).get("objectTypes") or []:
        if isinstance(ot, dict) and space in (ot.get("name"), ot.get("namespace")):
            return ot
    return None


def _current(index, type_name: str, record_id: str) -> dict | None:
    """The record `record_id` as it stands: its draft when there is one (the newer copy), else published."""
    for bucket in ("drafts", "objects"):
        for rec in ((index or {}).get(bucket) or {}).get(type_name) or []:
            if isinstance(rec, dict) and str(rec.get("id")) == record_id:
                # The node's read stamps _createdAt/_updatedAt/_version on; they are not the record's.
                return {k: v for k, v in rec.items() if not str(k).startswith("_")}
    return None


def write_record(agent_name: str, organism_id: str, ws: str, space: str, fields: dict, record_id: str = "") -> dict:
    """Create or update one record and publish it. Returns {id, space, namespace, created}.

    With `record_id` naming a record that exists, `fields` are merged onto it (a field set to None is
    removed); otherwise the record is created from `fields`, under `record_id` when one is given and under
    a new `<space>-<12 hex>` id when not -- the node makes no id for a record and refuses a write without
    one (measured on a local node: "A records write needs an id").
    Raises WorkspaceWriteError with the node's words when any step is refused.
    """
    if not isinstance(fields, dict) or not fields:
        raise WorkspaceWriteError("A record needs at least one field.")
    index = _aimeat_call(agent_name, "aimeat_workspace_read", {"organism_id": organism_id, "ws": ws}, return_error=True)
    why = _refused(index, f"read of workspace {ws}")
    if why:
        raise WorkspaceWriteError(why)
    ot = _object_type(index, space)
    if ot is None:
        types = (index.get("manifest") or {}).get("objectTypes") or []
        names = ", ".join(str(o.get("name")) for o in types if isinstance(o, dict))
        raise WorkspaceWriteError(f"Workspace {ws} has no space {space!r}. Its spaces: {names}.")
    if ot.get("backing") == "rows" or ot.get("mode") == "rows":
        raise WorkspaceWriteError(f"{space!r} is a row space; append to it with append_workspace_rows.")
    current = _current(index, str(ot.get("name")), record_id) if record_id else None
    value = dict(current or {})
    value.update(fields)
    value = {k: v for k, v in value.items() if v is not None}
    record_id = record_id or f"{ot.get('name')}-{uuid.uuid4().hex[:12]}"
    payload = {"organism_id": organism_id, "ws": ws, "space": ot.get("name"), "id": record_id, "value": value}
    written = _aimeat_call(agent_name, "aimeat_workspace_write", payload, return_error=True)
    why = _refused(written, f"write to {space}")
    if why:
        raise WorkspaceWriteError(why)
    rid = str((written or {}).get("id") or record_id)
    published = _aimeat_call(
        agent_name,
        "aimeat_workspace_publish",
        {"organism_id": organism_id, "ws": ws, "namespace": ot.get("namespace"), "id": rid},
        return_error=True,
    )
    why = _refused(published, f"publish of {space} {rid}")
    if why:
        # The draft landed and the app does not show it. Saying only "refused" would hide that half.
        raise WorkspaceWriteError(f"{why} The draft of {rid} was written but is not published, so apps do not show it.")
    return {"id": rid, "space": ot.get("name"), "namespace": ot.get("namespace"), "created": current is None}


def append_rows(agent_name: str, organism_id: str, ws: str, space: str, rows: list[dict]) -> dict:
    """Append rows to a row space; a row carrying the `row_id` of an existing row replaces it.

    Each row is {"body": {...}} with an optional "row_id" and "occurred_at". Returns the node's answer
    ({written, row_ids, pruned}); raises WorkspaceWriteError with the node's words when refused.
    """
    clean = []
    for i, r in enumerate(rows or []):
        if not isinstance(r, dict) or not isinstance(r.get("body"), dict):
            raise WorkspaceWriteError(f"rows[{i}] needs a `body` object.")
        clean.append({k: r[k] for k in ("body", "row_id", "occurred_at") if r.get(k) is not None})
    if not clean:
        raise WorkspaceWriteError("No rows to append.")
    answer = _aimeat_call(
        agent_name,
        "aimeat_workspace_rows_append",
        {"organism_id": organism_id, "ws": ws, "space": space, "rows": clean},
        return_error=True,
    )
    why = _refused(answer, f"append to {space}")
    if why:
        raise WorkspaceWriteError(why)
    return answer


def _json_arg(raw, what: str):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        raise WorkspaceWriteError(f"{what} is not valid JSON: {exc}") from exc


def make_workspace_write_tools(agent_name: str) -> list:
    """The crew_def `workspace_write` tool: the read tools, plus writing records and appending rows."""
    from crewai.tools import tool

    @tool("write_workspace_record")
    def write_workspace_record(
        workspace: str, space: str, fields_json: str, record_id: str = "", organism: str = ""
    ) -> str:
        """Create or update ONE record in a workspace's record space, and publish it so apps show it.
        `workspace` is named as list_workspaces shows it (e.g. "CADENCE"); `organism` only when two
        organisms have one of that name. `space` is the space's name from its index ("contact", "deal").
        `fields_json` is a JSON object of the fields to set. To UPDATE, pass the record's `record_id` and
        only the fields that change: they are merged onto the record as it stands (a field set to null is
        removed). To CREATE, leave `record_id` empty (an id is made) or pass a new one. Look the record up first so you
        do not create a duplicate. The answer says whether it was created or updated."""
        hit, why = resolve_workspace(agent_name, workspace, organism)
        if hit is None:
            return f"NOT WRITTEN. {why}"
        try:
            fields = _json_arg(fields_json, "fields_json")
            done = write_record(agent_name, hit["organism_id"], hit["ws"], space, fields, record_id)
        except WorkspaceWriteError as exc:
            return f"NOT WRITTEN. {exc}"
        verb = "Created" if done["created"] else "Updated"
        return f"{verb} and published {done['space']} {done['id']} in workspace {hit['name']}."

    @tool("append_workspace_rows")
    def append_workspace_rows(workspace: str, space: str, rows_json: str, organism: str = "") -> str:
        """Append rows to a workspace's ROW space (e.g. mail messages). `rows_json` is a JSON list of
        {"body": {...}, "row_id": optional, "occurred_at": optional ISO time}. A row whose `row_id`
        already exists is replaced, which is how a row is updated."""
        hit, why = resolve_workspace(agent_name, workspace, organism)
        if hit is None:
            return f"NOT WRITTEN. {why}"
        try:
            rows = _json_arg(rows_json, "rows_json")
            done = append_rows(
                agent_name, hit["organism_id"], hit["ws"], space, rows if isinstance(rows, list) else [rows]
            )
        except WorkspaceWriteError as exc:
            return f"NOT WRITTEN. {exc}"
        return f"Wrote {done.get('written', 0)} row(s) to {space} in workspace {hit['name']}: {done.get('row_ids')}."

    return [*make_workspace_tools(agent_name), write_workspace_record, append_workspace_rows]
