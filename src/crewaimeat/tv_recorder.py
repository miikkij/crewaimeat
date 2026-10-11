"""tv_recorder — what the TV-opas recorder does with a task, in code. No model is asked anything.

The contract is brief doc-mv2t4n84hsf3 (version 2), written by the session that builds the TV-opas app;
this module is the agent's half of it. The app creates a task whose `scope` carries everything as
structure (`kind`, `recording_id`, `stream_url`, `start`, `stop`, `quality`, ...), so there is nothing
to interpret and nothing for a model to get wrong:

  tvopas.record   wait until `start`, record to `stop`, compress to ONE MP4 (crewaimeat.stream_record),
                  upload it to the agent's OWN storage at `tvopas/rec/<recording_id>.mp4`, add it to the
                  catalogue, and close the task with one line a person can read.
  tvopas.delete   delete the file, remove the catalogue item.

Each stage is reported as a task event whose first word the app shows: `waiting`, `recording`,
`packing`, `uploading`. A failure closes the task as failed with its code first (`STREAM_UNREACHABLE`,
`GEO_BLOCKED`, `STREAM_ENDED_EARLY`, `NO_FFMPEG`, `TOO_LONG`, `QUOTA`, `UPLOAD_FAILED`, and the ones this
side added for what the brief's seven do not cover: `BAD_TASK` (a scope that cannot be read), `TOO_LATE`
(the programme was over before the task was reached), `RUN_LIMIT` (the agent runs one worker per job and
the job is longer than a worker lives), `CANCELLED`, `PACK_FAILED`, `RECORDER_ERROR`).

THE CATALOGUE is one memory record in the agent's own namespace, `tvopas.recordings`, and this process
is its only writer; the app reads it and never writes it. On every run, before anything else, the
recordings whose `keepUntil` has passed are deleted: a store that only grows is the failure here.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from crewaimeat import stream_record as sr
from crewaimeat.stream_record import RecordError

CATALOGUE_KEY = "tvopas.recordings"
FILE_PREFIX = "tvopas/rec/"
KIND_RECORD, KIND_DELETE = "tvopas.record", "tvopas.delete"
#: The brief: a `start` in the past or within a minute means now.
START_NOW_S = 60.0
#: The node marks an active task stalled after 120 minutes without an event (its
#: AIMEAT_TASK_STALL_THRESHOLD_MINUTES default). A long wait or a long film says where it is well inside that.
STILL_HERE_S = 30 * 60.0
#: How often a waiting or working task asks the NODE whether it has been ended: the task's status and
#: the app's cancel marker. Half a minute, so a Stop is obeyed within a minute even when the push for
#: it never arrived.
LOOK_S = 30.0
#: How often it reads the connector daemon's own list of cancelled tasks, which the node's push fills.
#: A loopback call that reaches no node, so it is asked often and a Stop is usually obeyed in seconds.
PEEK_S = 5.0
#: The record AIMEAT.agents.cancelTask writes in the owner's memory; its value is the list of task ids.
CANCEL_MARKER = "agents.cancel.task."
#: The least time between two notes about a stream that had to be attached again.
NOTE_S = 5 * 60.0
#: The brief: the recording id is the file's name; letters, digits, dot, dash and underscore only. It
#: also names a folder on this machine that is deleted afterwards, so it starts with a letter or a
#: digit: `..` is made of allowed characters and is not an id.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
# The catalogue is read, changed and written back. Two recordings ending in the same minute are two
# threads of this one process, so the read-change-write is one at a time.
_CATALOGUE_LOCK = threading.Lock()
_STARTED = time.monotonic()


@dataclass
class Request:
    kind: str
    recording_id: str
    stream_url: str = ""
    user_agent: str = ""
    referrer: str = ""
    start: datetime | None = None
    stop: datetime | None = None
    quality: str = sr.DEFAULT_QUALITY
    title: str = ""
    channel: str = ""
    keep_days: int = 0

    @property
    def file(self) -> str:
        return f"{FILE_PREFIX}{self.recording_id}.mp4"


def _call(agent: str, tool: str, payload: dict, **kw):
    from crewaimeat.aimeat_crew import _aimeat_call

    return _aimeat_call(agent, tool, payload, **kw)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clock(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%H:%M UTC")


def _time(value: str, name: str) -> datetime:
    try:
        t = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecordError("BAD_TASK", f"`{name}` is not a time ({value!r}); it is an ISO time in UTC.") from exc
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def scope_of(task: dict) -> dict[str, str]:
    """The task's scope as {name: value}. The node hands it over as a list of {name, value, type}."""
    raw = task.get("scope") or []
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return {str(s.get("name")): str(s.get("value") or "") for s in raw if isinstance(s, dict) and s.get("name")}


def parse_request(task: dict) -> Request:
    """Read a task into a Request, or say what is wrong with it. Nothing here is guessed."""
    scope = scope_of(task)
    kind = scope.get("kind", "").strip()
    if kind not in (KIND_RECORD, KIND_DELETE):
        raise RecordError(
            "BAD_TASK",
            f"this agent records and deletes TV-opas recordings; the task's scope `kind` is {kind or 'missing'!r}, "
            f"not {KIND_RECORD} or {KIND_DELETE}. It was made by something other than the TV-opas app.",
        )
    rid = scope.get("recording_id", "").strip()
    if not _ID.fullmatch(rid):
        raise RecordError("BAD_TASK", f"`recording_id` {rid!r} may hold letters, digits, . - and _ only.")
    if kind == KIND_DELETE:
        return Request(kind=kind, recording_id=rid)
    missing = [n for n in ("stream_url", "start", "stop") if not scope.get(n, "").strip()]
    if missing:
        raise RecordError("BAD_TASK", f"the task's scope lacks {', '.join(missing)}.")
    quality = (scope.get("quality") or sr.DEFAULT_QUALITY).strip().lower()
    if quality not in sr.QUALITIES:
        raise RecordError("BAD_TASK", f"`quality` is {quality!r}; it is one of {', '.join(sr.QUALITIES)}.")
    keep = (scope.get("keep_days") or "0").strip()
    if not keep.isdigit():
        raise RecordError("BAD_TASK", f"`keep_days` is {keep!r}; it is a whole number of days, 0 to keep until asked.")
    req = Request(
        kind=kind,
        recording_id=rid,
        stream_url=scope["stream_url"].strip(),
        user_agent=scope.get("user_agent", "").strip(),
        referrer=scope.get("referrer", "").strip(),
        start=_time(scope["start"], "start"),
        stop=_time(scope["stop"], "stop"),
        quality=quality,
        title=scope.get("title", "").strip() or rid,
        channel=scope.get("channel", "").strip(),
        keep_days=int(keep),
    )
    if req.stop <= req.start:
        raise RecordError("BAD_TASK", f"`stop` ({_iso(req.stop)}) is not after `start` ({_iso(req.start)}).")
    return req


# --------------------------------------------------------------------------- the catalogue


def read_catalogue(agent: str) -> dict:
    """The catalogue as it stands. Raises when the node did not say: an unread catalogue written back
    would be an empty one, and every recording in it would be lost from the app's list."""
    r = _call(agent, "aimeat_memory_read", {"key": CATALOGUE_KEY}, return_error=True, quiet=True)
    if r is None:
        raise RecordError("UPLOAD_FAILED", f"the node did not answer the read of the catalogue ({CATALOGUE_KEY}).")
    if isinstance(r, dict) and r.get("ok") is False:
        err = r.get("error")
        code = str((err.get("code") if isinstance(err, dict) else err) or "").upper()
        if code == "NOT_FOUND" or r.get("http_status") == 404:
            return {"v": 1, "items": []}
        raise RecordError("UPLOAD_FAILED", f"the node refused the read of the catalogue ({CATALOGUE_KEY}): {err}.")
    value = r.get("value") if isinstance(r, dict) else None
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        return {"v": 1, "items": []}
    return value


def write_catalogue(agent: str, items: list[dict]) -> dict:
    doc = {
        "v": 1,
        "updatedAt": _iso(_now()),
        "storedBytes": sum(int(i.get("bytes") or 0) for i in items),
        "items": items,
    }
    r = _call(agent, "aimeat_memory_write", {"key": CATALOGUE_KEY, "value": doc, "visibility": "private"})
    if r is None:
        raise RecordError("UPLOAD_FAILED", f"the node did not take the catalogue ({CATALOGUE_KEY}).")
    return doc


def _forget(agent: str, ids: set[str]) -> list[dict]:
    """Delete the files of `ids` and take them out of the catalogue. Returns the items removed. A file
    the node would not delete stays in the catalogue, so it is still listed and is tried again."""
    with _CATALOGUE_LOCK:
        items = read_catalogue(agent)["items"]
        gone = [i for i in items if i.get("id") in ids and sr.delete_file(agent, str(i.get("file") or ""))]
        if gone:
            write_catalogue(agent, [i for i in items if i not in gone])
        return gone


def purge_expired(agent: str) -> list[dict]:
    """Delete every recording whose `keepUntil` has passed. Run before anything else, on every run."""
    now = _now()
    with _CATALOGUE_LOCK:
        items = read_catalogue(agent)["items"]
    due = {str(i["id"]) for i in items if i.get("keepUntil") and _time(str(i["keepUntil"]), "keepUntil") <= now}
    gone = _forget(agent, due) if due else []
    for i in gone:
        print(f"[{agent}] kept its {i.get('keepUntil')} limit: deleted {i.get('file')}", file=sys.stderr)
    return gone


def delete_recording(agent: str, rid: str) -> str:
    """A `tvopas.delete` task: no file and no catalogue item afterwards."""
    file = f"{FILE_PREFIX}{rid}.mp4"
    gone = _forget(agent, {rid})
    if gone:
        return f"Deleted {gone[0].get('title') or rid} ({sr.megabytes(int(gone[0].get('bytes') or 0))})."
    with _CATALOGUE_LOCK:
        listed = any(i.get("id") == rid for i in read_catalogue(agent)["items"])
    if listed:
        raise RecordError("UPLOAD_FAILED", f"the node would not delete {file}; the recording is still there.")
    # Not in the catalogue. A file may still be there from a run that stopped between the upload and
    # the catalogue; deleting by name covers it, and a file that never was is deleted already.
    if not sr.delete_file(agent, file):
        raise RecordError("UPLOAD_FAILED", f"the node would not delete {file}.")
    return f"Nothing to delete: there is no recording {rid}."


# --------------------------------------------------------------------------- one recording


def _pushed_cancels(agent: str) -> set[str]:
    """The task ids the node has pushed to this agent as cancelled, as the connector daemon holds them
    (GET /local/cancelled). Empty when there is no daemon or it will not say: this is the fast road,
    and the marker on the node is the one that is relied on."""
    from crewaimeat.aimeat_crew import _serve_api

    api = _serve_api()  # discovery only; the session carries the daemon's secret
    if api is None:
        return set()
    base, session = api
    try:
        r = session.get(
            f"{base}/local/cancelled", params={"agent": agent}, headers={"X-Aimeat-Agent": agent}, timeout=10
        )
        if r.status_code != 200:
            return set()
        return {str(i) for i in ((r.json() or {}).get("data") or {}).get("cancelled") or []}
    except Exception:  # noqa: BLE001 -- no list is not a cancellation; the marker read still decides
        return set()


def _marked_cancelled(agent: str, task_id: str) -> bool:
    """Whether the owner's memory holds the cancel marker for this task: the record
    `agents.cancel.task.<taskId>` that AIMEAT.agents.cancelTask writes, its value the list of ids."""
    r = _call(
        agent,
        "aimeat_memory_read",
        {"key": f"{CANCEL_MARKER}{task_id}", "owner_scope": True},
        return_error=True,
        quiet=True,
    )
    if not isinstance(r, dict) or r.get("ok") is False:
        return False
    value = r.get("value")
    return isinstance(value, (list, dict)) and task_id in {str(i) for i in value}


class _Stages:
    """Says each stage on the task as it begins, repeats where it is every half hour so the node does
    not call a long film stalled, and notices when the task has been ended or cancelled."""

    def __init__(self, agent: str, task_id: str):
        self.agent, self.task_id = agent, task_id
        self._stage, self._detail, self._since = "", "", 0.0
        self._said = self._looked = self._peeked = time.monotonic()
        self._noted = 0.0
        self._ended = False

    def say(self, stage: str, detail: str = "") -> None:
        self._stage, self._detail = stage, detail
        self._since = self._said = time.monotonic()
        self._event(f"{stage} {detail}".strip())

    def note(self, text: str) -> None:
        """Something the person should see inside the current stage (the stream dropped and is being
        attached again). The first is said at once; after that at most one in five minutes, because a
        stream that flaps would otherwise fill the task's timeline."""
        now = time.monotonic()
        if self._noted and now - self._noted < NOTE_S:
            return
        self._noted = self._said = now
        self._event(f"{self._stage} {self._detail}: {text}")

    def _event(self, message: str) -> None:
        r = _call(self.agent, "aimeat_task_event", {"task_id": self.task_id, "type": "progress", "message": message})
        print(
            f"[{self.agent}] task {self.task_id}: {message}{'' if r is not None else '  (event not taken)'}",
            file=sys.stderr,
        )

    def _stopped(self, why: str) -> bool:
        self._ended = True
        print(f"[{self.agent}] task {self.task_id}: stopping, {why}", file=sys.stderr)
        return True

    def ended(self, *, force: bool = False) -> bool:
        """True once somebody has ended this task. Not knowing is not ended.

        THREE WAYS A RECORDING IS STOPPED, because the app and the owner do it differently:
          the push    the app calls AIMEAT.agents.cancelTask, which writes a marker in the owner's
                      memory; the node pushes `task.cancelled` down the tunnel and the connector
                      daemon keeps the id. Read here every PEEK_S: a loopback call, no node traffic.
          the marker  the same cancel, read from the node every LOOK_S. The push is sent once, to an
                      agent that is online at that moment, and a daemon that restarts forgets its
                      list; the marker is what is still there afterwards.
          the status  the owner in person pauses, fails or deletes the task. An app cannot do that
                      (owner only), which is why the status alone did not let the app stop anything.
        `force` looks now, whatever was looked at a moment ago: before the upload and after it."""
        if self._ended:
            return True
        now = time.monotonic()
        if force or now - self._peeked >= PEEK_S:
            self._peeked = now
            if self.task_id in _pushed_cancels(self.agent):
                return self._stopped("the app cancelled it (pushed by the node)")
        if not force and now - self._looked < LOOK_S:
            return False
        self._looked = now
        got = _call(self.agent, "aimeat_task_get", {"task_id": self.task_id}, quiet=True)
        task = (got.get("task") if isinstance(got, dict) and isinstance(got.get("task"), dict) else got) or {}
        status = str(task.get("status") or "") if isinstance(task, dict) else ""
        if status and status not in ("active", "stalled"):
            return self._stopped(f"the task is {status} on the node")
        if _marked_cancelled(self.agent, self.task_id):
            return self._stopped(f"the app cancelled it (marker {CANCEL_MARKER}{self.task_id})")
        if now - self._said >= STILL_HERE_S:
            self._said = now
            self._event(f"{self._stage} {self._detail} ({sr.span(now - self._since)} so far)".replace("  ", " "))
        return False


def _run_limit(seconds_from_now: float) -> None:
    """Refuse a recording a per-wake worker would be ended in the middle of, since a recording stopped
    there is lost with nothing saying why.

    The spawner tells its worker the limit it runs under (AIMEAT_SPAWN_RUN_TIMEOUT_S, 0 = none). This
    crew declares none (RUN_TIMEOUT_S = 0), so under this repo's spawner nothing is refused here. What
    is left is a worker somebody else started with a limit: a spawner older than the per-agent limit
    says nothing, and then its hour (SPAWN_RUN_TIMEOUT_S) is what applies."""
    if os.environ.get("AIMEAT_SPAWN_WORKER") != "1":
        return
    told = os.environ.get("AIMEAT_SPAWN_RUN_TIMEOUT_S")
    limit = float(told) if told is not None else float(os.environ.get("SPAWN_RUN_TIMEOUT_S") or 3600)
    if limit <= 0:
        return
    left = limit - (time.monotonic() - _STARTED)
    need = seconds_from_now * 1.25 + 120  # the wait and the recording, then compressing and uploading it
    if need > left:
        raise RecordError(
            "RUN_LIMIT",
            f"this recorder is started once per job and stopped after {sr.span(limit)}, and this recording "
            f"ends in {sr.span(seconds_from_now)}. Whoever runs it gives the agent a longer limit, or none, "
            f"or sets it to run always on; then a recording may be as long as the programme.",
        )


def record_task(agent: str, task_id: str, req: Request) -> str:
    """A `tvopas.record` task, from the wait to the catalogue. Returns the task's closing line."""
    now = _now()
    if req.stop <= now:
        raise RecordError(
            "TOO_LATE",
            f"the programme ended at {_clock(req.stop)}, before the recorder got to it; nothing was recorded.",
        )
    sr.find_ffmpeg()  # before a wait of hours, not after it
    limit = int(sr.open_upload(agent, req.file).get("max_size_bytes") or 0) or None
    begin = req.start if (req.start - now).total_seconds() > START_NOW_S else now
    planned = (req.stop - begin).total_seconds()
    sr.check_fits(planned, limit)
    _run_limit((req.stop - now).total_seconds())
    stages = _Stages(agent, task_id)
    what = f"{req.title}, {req.channel}" if req.channel else req.title

    if begin > now:
        stages.say("waiting", f"until {_clock(begin)} to record {what}")
        while _now() < begin:
            if stages.ended():
                raise RecordError("CANCELLED", "the task was ended before the recording began.")
            time.sleep(min(5.0, max(0.0, (begin - _now()).total_seconds())))

    seconds = (req.stop - _now()).total_seconds()
    work = sr.workdir_for(req.recording_id)
    shutil.rmtree(work, ignore_errors=True)
    stored = False
    try:

        def on_stage(stage: str) -> None:
            stages.say(stage, f"{what} until {_clock(req.stop)}" if stage == "recording" else what)

        rec = sr.record(
            req.stream_url,
            seconds,
            work,
            quality=req.quality,
            max_bytes=limit,
            user_agent=req.user_agent,
            referrer=req.referrer,
            should_stop=stages.ended,
            on_stage=on_stage,
            on_note=stages.note,
        )
        if stages.ended(force=True):
            raise RecordError("CANCELLED", "the recording was stopped before it was stored; nothing was kept.")
        stages.say("uploading", f"{what}, {sr.megabytes(rec.bytes)}")
        sent = sr.store_file(agent, rec.path, req.file)
        stored = True
        if stages.ended(force=True):
            # Stopped while the file was on its way. It is stored now, and the handler below removes it.
            raise RecordError("CANCELLED", "the recording was stopped while it was being stored; it was removed again.")
        done = _now()
        item: dict[str, Any] = {
            "id": req.recording_id,
            "taskId": task_id,
            "title": req.title,
            "channel": req.channel,
            "start": _iso(req.start),
            "stop": _iso(req.stop),
            "quality": req.quality,
            "width": rec.width,
            "height": rec.height,
            "seconds": rec.seconds,
            "bytes": rec.bytes,
            "file": req.file,
            "recordedAt": _iso(done),
        }
        if req.keep_days > 0:
            item["keepUntil"] = _iso(done + timedelta(days=req.keep_days))
        with _CATALOGUE_LOCK:
            items = [i for i in read_catalogue(agent)["items"] if i.get("id") != req.recording_id]
            write_catalogue(agent, [*items, item])
    except Exception:
        if stored:
            # Stored and not listed is a file nobody will ever delete; it goes again.
            sr.delete_file(agent, req.file)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(
        f"[{agent}] {req.recording_id}: {sr.megabytes(rec.bytes)} uploaded in {sent['seconds']} s by {sent['route']}",
        file=sys.stderr,
    )
    notes = list(rec.notes)
    late = (now - req.start).total_seconds()
    if late > START_NOW_S:
        # The task was reached after the programme began (the app made it late, or the fleet was
        # restarted in the middle of the recording and this is the second go). A length alone does
        # not say that the beginning is not there.
        notes.insert(0, f"the first {sr.span(late)} are missing: the recorder got to it after the start")
    line = f"{what}, {sr.span(rec.seconds)}, {sr.megabytes(rec.bytes)}"
    return line + "".join(f"; {n}" for n in notes)


def handle_task(agent: str, task: dict) -> str:
    """DETERMINISTIC EXECUTE (CrewSpec.on_task). Does the task and tells the completion how it ended:
    the closing line a person reads, or the failure with its code first."""
    from crewaimeat.lifecycle import note_outcome

    task_id = str(task.get("id") or "")
    try:
        purge_expired(agent)
    except Exception as exc:  # noqa: BLE001 -- a clean-up that failed is said, and does not cost the recording its start
        print(
            f"[{agent}] the clean-up of expired recordings failed and is tried on the next run: {exc}", file=sys.stderr
        )
    try:
        req = parse_request(task)
        line = (
            delete_recording(agent, req.recording_id) if req.kind == KIND_DELETE else record_task(agent, task_id, req)
        )
    except RecordError as exc:
        note_outcome(task_id, str(exc), failed=True)
        return str(exc)
    except Exception as exc:  # noqa: BLE001 -- the task carries the reason; a stack in a log reaches nobody
        reason = f"RECORDER_ERROR: the recorder stopped on an error it did not expect ({type(exc).__name__}: {exc})."
        note_outcome(task_id, reason, failed=True)
        return reason
    note_outcome(task_id, line)
    return line
