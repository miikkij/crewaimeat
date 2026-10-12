"""A record published in a watched workspace space becomes a TASK the agent runs.

This is what a crew definition's `record_spaces` means at run time. A Python contract crew hands the
daemon its own `on_record`, which scans the space and works on the records its contract's lifecycle
marks as waiting. A definition has no lifecycle to read, so the runtime does the same scan in code and
hands the crew ONE record at a time, as a task.

WHY A REAL TASK AND NOT A CREW RUN ON THE EVENT. Everything this runtime knows about showing work to
its owner is built on tasks: the plan, the result on the agent's page, the failed state with its
reason, the permission the node refused named in words, the re-run of a worker that died, the cost
under one id, and "where does its work go". A crew started straight from the event has none of it: a
model that is down or a write the node refused would leave a log line on one computer and a record
nobody answered. And a task follows the owner's own start rule: a task-runner's starts at once, any
other agent's waits for a person, exactly like its other work. The agent creates the task for itself
(`POST /v1/agents/<name>/tasks`, which the node allows an agent for itself and its owner's other
agents; measured on a local node 2026-10-12: 201 and `active` for a task-runner).

EVERY EVENT IS A REASON TO READ THE SPACE, NEVER THE WORK ITSELF. The event names one record, and in
a spawned worker a catch-up event for each space also arrives on every start (the daemon's reconnect
count starts at -1 in a new process). So one rule serves both: read the space's index and make a task
for each published record that is newer than what this agent has handled. What it has handled is a
small memo on the NODE, under the agent's name, so it moves with the agent to another computer.

THE FIRST LOOK AT A SPACE HANDLES NOTHING. The memo starts at the newest record that is there. An
agent switched on over a workspace of five hundred old requests must not run five hundred tasks; it
works on what is published from then on. The one exception is the record a real event names: that
one was published while this agent was subscribed.

AN AGENT'S OWN WRITE DOES NOT WAKE IT AGAIN. The node's event carries no author and is pushed to the
writer too (aimeat-protocol connect-tunnel-push.ts), so an agent that writes into a space it watches
would start itself for ever. Before a task is made the record is read, and one whose provenance names
this agent as its generator is marked handled with no task.

A STEP THAT COULD NOT BE DONE RAISES `RecordWakeError`. Nothing is assumed from a refused or missing
answer: a memo that could not be read is not "nothing handled yet", which would be the five hundred
tasks again.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from crewaimeat.agent_manifest import agent_local_name, agent_route

# Where the agent keeps what it has handled, per watched space. Keyed by the agent NAME (see
# crew_registry.registry_key): the identity is in the transport, never in the key.
STATE_PREFIX = "crews.records."

# How far behind the mark a record is still looked at. The node stamps a write before it is stored, so
# two writes a moment apart can become visible in the other order: the later-stamped one is read, the
# mark moves past both, and the earlier-stamped one appears afterwards. Records inside this window are
# remembered one by one (`recent`), so a late one is still new and a handled one is not repeated.
ORDER_SLACK = timedelta(seconds=60)


class RecordWakeError(RuntimeError):
    """A read or write this needs was refused or not answered. The message is the node's own words.
    `refused` tells the node's verdict (which stands until the owner changes something) from an answer
    that did not arrive (which the next scan simply tries again)."""

    def __init__(self, message: str, *, refused: bool = False) -> None:
        super().__init__(message)
        self.refused = refused


def state_key(agent_name: str) -> str:
    return f"{STATE_PREFIX}{agent_local_name(agent_name)}"


def space_key(space: dict) -> str:
    return f"{space.get('organism_id')}/{space.get('ws')}/{space.get('space')}"


def _when(stamp: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None


def new_records(entries: list[dict], memo: dict | None) -> tuple[list[dict], dict]:
    """`(the published records not yet handled, oldest first; the memo before any of them is handled)`.

    Pure. `entries` are the index rows of one space ({id, updated, version, published}); `memo` is
    {"mark": the newest handled `updated`, "recent": {id: its `updated`} for the records within
    ORDER_SLACK of the mark}, or None when this agent has never looked at the space. A first look
    returns no records and a memo that starts at the newest one.

    A RECORD IS KNOWN BY WHEN IT WAS PUBLISHED, NOT BY ITS VERSION NUMBER. The index's `version` did
    not move when a record first written as a bare key was published again through the workspace
    (measured on a local node 2026-10-12: `updated` went from 16:03:43 to 16:04:33 and `version`
    stayed 1), so a memo of versions took that publish for one already handled. `updated` changes on
    every publish.
    """
    rows = [
        e for e in entries if isinstance(e, dict) and e.get("id") and e.get("published") and _when(e.get("updated"))
    ]
    rows.sort(key=lambda e: (_when(e["updated"]), str(e["id"])))
    if not isinstance(memo, dict):
        mark = rows[-1]["updated"] if rows else ""
        return [], _pruned({"mark": mark, "recent": {str(e["id"]): e["updated"] for e in rows}})
    mark, recent = _when(memo.get("mark")), dict(memo.get("recent") or {})
    floor = mark - ORDER_SLACK if mark else None
    fresh = [
        e for e in rows if (floor is None or _when(e["updated"]) > floor) and recent.get(str(e["id"])) != e["updated"]
    ]
    return fresh, {"mark": str(memo.get("mark") or ""), "recent": recent}


def handled(memo: dict, record_id: str, updated: str) -> dict:
    """The memo once the record published at `updated` is handled: remembered, and the mark moved up."""
    recent = {**(memo.get("recent") or {}), str(record_id): updated}
    mark, at = _when(memo.get("mark")), _when(updated)
    newest = updated if at and (mark is None or at > mark) else str(memo.get("mark") or "")
    return _pruned({"mark": newest, "recent": recent})


def _pruned(memo: dict) -> dict:
    """`recent` cut to the records published within ORDER_SLACK of the mark; older ones are behind it."""
    mark = _when(memo.get("mark"))
    if mark is None:
        return memo
    near = {i: at for i, at in memo["recent"].items() if (t := _when(at)) and t > mark - ORDER_SLACK}
    return {"mark": memo["mark"], "recent": near}


def _writer(item: dict) -> str | None:
    """Who the node recorded as the generator of this record's current version, when it recorded one."""
    prov = item.get("ai_provenance")
    record = prov.get("record") if isinstance(prov, dict) else None
    generator = record.get("generator") if isinstance(record, dict) else None
    principal = generator.get("principal") if isinstance(generator, dict) else None
    return str(principal) if principal else None


def own_identity(agent_name: str) -> str:
    """This agent's GAII: the identity as handed when it is one, else read from its credential here.
    Never assembled from a name: two owners may each have an agent of that name."""
    if "#" in str(agent_name):
        return str(agent_name)
    from aimeat_crewai.daemon import resolve_agent_identity

    return resolve_agent_identity(str(agent_name)).gaii


def task_for(space: dict, type_name: str, ws_name: str, item: dict) -> dict:
    """The task a published record becomes. Its description is what `{{ctx.prompt}}` gives the crew, so
    the text is a contract with whoever writes a definition's task: change it there too
    (aimeat-protocol, the home's draft prompt)."""
    # Created or published again, by the record's own two times: its version number is not a count
    # of publishes (see new_records).
    version, born = item.get("_version"), item.get("_createdAt")
    op = "updated" if born and born != item.get("_updatedAt") else "created"
    org, ws, namespace, rid = space["organism_id"], space["ws"], space["space"], str(item["id"])
    description = (
        f"A record was {op} in a workspace space this agent watches.\n\n"
        f"workspace: {ws_name} (id {ws})\n"
        f"organism: {org}\n"
        f"space: {type_name} (namespace {namespace})\n"
        f"record id: {rid}\n"
        f"record version: {version}, published {item.get('_updatedAt')}\n\n"
        "The record as published:\n"
        f"{json.dumps(item.get('value'), ensure_ascii=False, indent=1)}\n\n"
        f'To read or write in this workspace with your tools, name it by its id: workspace="{ws}", '
        f'organism="{org}". A space is named by its name, for example space="{type_name}".'
    )
    return {
        "title": f"Workspace record {op}: {type_name}/{rid}",
        "description": description,
        # One publish of a record is one piece of work: a second create while the first task is still
        # open answers with that task (the node's one-live-commission guard).
        "idempotency_key": f"record:{org}/{ws}/{namespace}/{rid}@{item.get('_updatedAt')}",
    }


class RecordWake:
    """The scan of one agent's watched spaces. `call` and `rest` are the node transports
    (`_aimeat_call`, `_aimeat_rest`), handed in so a test drives this with a double of the node."""

    def __init__(self, agent_name: str, *, call=None, rest=None, say=None, identity=None) -> None:
        if call is None or rest is None:
            from crewaimeat.aimeat_crew import _aimeat_call, _aimeat_rest

            call, rest = call or _aimeat_call, rest or _aimeat_rest
        self.agent_name = agent_name
        self._call, self._rest = call, rest
        self._say = say or (lambda line: print(line, flush=True))
        self._identity = identity

    # ------------------------------------------------------------------ node --- #
    def _ask(self, answer: Any, what: str) -> dict:
        if answer is None:
            raise RecordWakeError(f"the node did not answer the {what}")
        if isinstance(answer, dict) and answer.get("ok") is False:
            err = answer.get("error") if isinstance(answer.get("error"), dict) else {}
            raise RecordWakeError(
                f"the node refused the {what} ({err.get('code', 'refused')}): {err.get('message', '')}".rstrip(": "),
                refused=True,
            )
        if not isinstance(answer, dict):
            raise RecordWakeError(f"the {what} came back as {type(answer).__name__}, not an answer to read")
        return answer

    def read_state(self) -> dict:
        from crewaimeat.memory_tools import read_owner_key_strict

        value = read_owner_key_strict(self.agent_name, state_key(self.agent_name), needs="memory:read")
        if isinstance(value, str):
            value = json.loads(value)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise RecordWakeError(f"{state_key(self.agent_name)} holds {type(value).__name__}, not this agent's memo")
        return dict(value.get("spaces") or {})

    def write_state(self, spaces: dict) -> None:
        payload = {"key": state_key(self.agent_name), "value": {"spaces": spaces}, "visibility": "owner"}
        self._ask(
            self._call(
                self.agent_name, "aimeat_memory_write", {**payload, "tags": ["crew-records"]}, return_error=True
            ),
            f"write of {state_key(self.agent_name)}",
        )

    def _index(self, space: dict) -> tuple[dict, str, list[dict]]:
        """`(the object type of this namespace, the workspace's name, its index rows)`."""
        where = {"organism_id": space["organism_id"], "ws": space["ws"]}
        index = self._ask(
            self._call(self.agent_name, "aimeat_workspace_read", where, return_error=True),
            f"read of workspace {space['ws']}",
        )
        manifest = index.get("manifest") if isinstance(index.get("manifest"), dict) else {}
        kinds = [o for o in manifest.get("objectTypes") or [] if isinstance(o, dict)]
        kind = next((o for o in kinds if o.get("namespace") == space["space"]), None)
        if kind is None:
            names = ", ".join(str(o.get("namespace")) for o in kinds)
            raise RecordWakeError(
                f"workspace {space['ws']} has no space with the namespace {space['space']!r} (it has: {names}); "
                "correct `record_spaces` in the agent's definition"
            )
        rows = (index.get("index") or {}).get(kind.get("name"))
        if not isinstance(rows, list):
            raise RecordWakeError(
                f"the read of workspace {space['ws']} came back without the index of {kind.get('name')!r}, so "
                "which records are new is not known"
            )
        return kind, str(manifest.get("name") or space["ws"]), rows

    def _record(self, space: dict, type_name: str, record_id: str) -> dict | None:
        """The record as published, with the node's own fields beside it. None when it is gone."""
        payload = {"organism_id": space["organism_id"], "ws": space["ws"], "ids": [record_id], "space": type_name}
        answer = self._ask(
            self._call(self.agent_name, "aimeat_workspace_read", payload, return_error=True),
            f"read of {type_name} {record_id}",
        )
        items = answer.get("items")
        if not isinstance(items, list):
            raise RecordWakeError(f"the read of {type_name} {record_id} came back without `items`")
        for item in items:
            if isinstance(item, dict) and str(item.get("id")) == record_id and item.get("space") in (None, type_name):
                return item if item.get("published") else None
        return None

    def _create_task(self, task: dict) -> dict:
        answer = self._ask(
            self._rest(self.agent_name, "POST", agent_route(self.agent_name, "/tasks"), task, return_error=True),
            f"creation of the task {task['title']!r}",
        )
        made = answer.get("task") if isinstance(answer.get("task"), dict) else answer
        return {"id": made.get("id"), "status": made.get("status"), "deduplicated": bool(answer.get("deduplicated"))}

    # ------------------------------------------------------------------ scan --- #
    def scan(self, space: dict, *, named: str | None = None) -> list[dict]:
        """Make a task for each record of `space` this agent has not handled. Returns what it did, one
        {id, updated, task | skipped} per record. `named` is the id a real event carried: it is handled
        even on the first look at a space."""
        key = space_key(space)
        spaces = self.read_state()
        kind, ws_name, rows = self._index(space)
        type_name = str(kind.get("name"))
        first_look = key not in spaces
        fresh, memo = new_records(rows, spaces.get(key))
        if first_look and named:
            fresh = [e for e in rows if isinstance(e, dict) and str(e.get("id")) == named and e.get("published")]
        if first_look:
            self._say(
                f"[{self.agent_name}] records: first look at {key}; {len(rows)} record(s) there are taken as "
                "handled, and the ones published from now on become tasks"
            )
        done: list[dict] = []
        try:
            for entry in fresh:
                done.append(self._handle(space, type_name, ws_name, entry))
                memo = handled(memo, done[-1]["id"], done[-1]["updated"])
        finally:
            # Saved also when a record could not be handled: the ones before it are done, and saying so
            # is what keeps the next scan from making their tasks again.
            if first_look or done:
                spaces[key] = memo
                self.write_state(spaces)
        return done

    def _handle(self, space: dict, type_name: str, ws_name: str, entry: dict) -> dict:
        rid = str(entry["id"])
        item = self._record(space, type_name, rid)
        if item is None:
            self._say(f"[{self.agent_name}] records: {type_name}/{rid} is no longer published; no task")
            return {"id": rid, "updated": entry["updated"], "skipped": "gone"}
        # The read is the newer word when the record was published again between the index and it.
        updated = str(item.get("_updatedAt") or entry["updated"])
        if self._identity is None:
            self._identity = own_identity(self.agent_name)
        if _writer(item) == self._identity:
            self._say(f"[{self.agent_name}] records: {type_name}/{rid} of {updated} is this agent's own write; no task")
            return {"id": rid, "updated": updated, "skipped": "own write"}
        made = self._create_task(task_for(space, type_name, ws_name, item))
        again = " (the node already had it open)" if made["deduplicated"] else ""
        self._say(
            f"[{self.agent_name}] records: {type_name}/{rid} of {updated} -> task {made['id']} ({made['status']}){again}"
        )
        return {"id": rid, "updated": updated, "task": made["id"]}

    def on_event(self, event: dict, watched: list[dict]) -> list[dict]:
        """One `workspace.record` event from the daemon. An event for a space the definition does not
        name is not this agent's work and is said, not acted on."""
        space = {k: event.get(k) for k in ("organism_id", "ws", "space")}
        if space not in [{k: w.get(k) for k in ("organism_id", "ws", "space")} for w in watched]:
            self._say(
                f"[{self.agent_name}] records: an event for {space_key(space)}, which the definition does not watch"
            )
            return []
        rid = event.get("id")
        return self.scan(space, named=str(rid) if rid and event.get("op") != "catchup" else None)
