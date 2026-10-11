"""tv-opas-recorder: a task from the TV-opas app becomes one stored MP4 and one catalogue item.

The contract is brief doc-mv2t4n84hsf3 (version 2). What is held here is the agent's half of it: the
scope is read and never guessed, the stages are said in order, the catalogue is written by this agent
alone and never from an unread copy, a recording that expired leaves by itself, and a failure closes
the task as failed with its code first. The node is stood in for at the two seams every call goes
through (`_aimeat_call`, and stream_record's upload); ffmpeg is stood in for by `stream_record.record`,
which has its own tests against a real stream.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import crewaimeat.stream_record as sr
import crewaimeat.tv_recorder as tv
from crewaimeat import lifecycle
from crewaimeat.lifecycle import LifecycleCallbacks

AGENT = "tv-opas-recorder"
_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 11, 18, 0, 0, tzinfo=timezone.utc)


def _z(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _task(tid="t-1", **over) -> dict:
    scope = {
        "kind": "tvopas.record",
        "recording_id": "FI-Nelonen.fi-29861820",
        "stream_url": "https://streams.example/nelonen/index.m3u8",
        "user_agent": "Mozilla/5.0 test",
        "referrer": "",
        "start": _z(NOW),
        "stop": _z(NOW + timedelta(minutes=95)),
        "quality": "low",
        "title": "Petolliset",
        "channel": "Nelonen",
        "keep_days": "14",
    }
    scope.update(over)
    return {
        "id": tid,
        "title": "Record Petolliset",
        "scope": [{"name": k, "value": v, "type": "text"} for k, v in scope.items() if v is not None],
    }


class Node:
    """The node, as far as this agent sees it: memory, storage, task events and the task's status."""

    def __init__(self):
        self.memory: dict[str, dict] = {}
        self.visibility: dict[str, str] = {}
        self.files: dict[str, int] = {}
        self.events: list[str] = []
        self.status = "active"
        self.answers: dict[str, object] = {}  # tool -> a fixed answer that overrides the behaviour below
        self.limit = 1024 * 2**20

    def call(self, agent, tool, payload, **kw):
        if tool in self.answers:
            return self.answers[tool]
        missing = {"ok": False, "error": {"code": "NOT_FOUND"}, "http_status": 404} if kw.get("return_error") else None
        if tool == "aimeat_memory_read":
            key = payload["key"]
            return (
                {"key": key, "exists": True, "value": json.loads(json.dumps(self.memory[key]))}
                if key in self.memory
                else missing
            )
        if tool == "aimeat_memory_write":
            self.memory[payload["key"]] = json.loads(json.dumps(payload["value"]))
            self.visibility[payload["key"]] = payload["visibility"]
            return {"key": payload["key"]}
        if tool == "aimeat_task_event":
            assert payload["type"] == "progress"
            self.events.append(payload["message"])
            return {"event": {"id": len(self.events)}}
        if tool == "aimeat_task_get":
            return {"task": {"id": payload["task_id"], "status": self.status}}
        if tool == "aimeat_storage_delete":
            return {"deleted": True} if self.files.pop(payload["key"], None) is not None else missing
        raise AssertionError(f"unexpected tool {tool}")

    def stages(self) -> list[str]:
        return [e.split(" ", 1)[0] for e in self.events]


@pytest.fixture
def node(monkeypatch, tmp_path):
    n = Node()
    # run_once marks its process as a spawn worker in os.environ, and its tests run in this process too.
    monkeypatch.delenv("AIMEAT_SPAWN_WORKER", raising=False)
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", n.call)
    monkeypatch.setattr(sr, "find_ffmpeg", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(
        sr, "open_upload", lambda agent, key, **k: {"upload_url": "http://node/u", "max_size_bytes": n.limit}
    )
    monkeypatch.setattr(sr, "workdir_for", lambda name: tmp_path / "work" / name)

    def store(agent, path, key, **k):
        n.files[key] = path.stat().st_size
        return {"bytes": path.stat().st_size, "seconds": 3.1, "route": "put"}

    monkeypatch.setattr(sr, "store_file", store)
    monkeypatch.setattr(tv, "_now", lambda: NOW)
    lifecycle._OUTCOMES.clear()
    yield n
    lifecycle._OUTCOMES.clear()


@pytest.fixture
def recorded(monkeypatch):
    """Stand in for ffmpeg: `record` leaves a file of the size a 95-minute programme at low comes to."""
    asked: list[dict] = []

    def fake(url, seconds, workdir, **kw):
        asked.append({"url": url, "seconds": seconds, **kw})
        kw["on_stage"]("recording")
        kw["on_stage"]("packing")
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / "recording.mp4"
        with open(out, "wb") as fh:
            fh.truncate(248_000_000)
        return sr.Recording(out, 248_000_000, 5700, 640, 360, kw["quality"], 300, 48, int(seconds))

    monkeypatch.setattr(sr, "record", fake)
    return asked


# ── the scope is read, never guessed ────────────────────────────────────────────────────────────


def test_a_record_task_is_read_from_its_scope():
    req = tv.parse_request(_task())
    assert (req.kind, req.recording_id, req.quality, req.keep_days) == (
        "tvopas.record",
        "FI-Nelonen.fi-29861820",
        "low",
        14,
    )
    assert req.file == "tvopas/rec/FI-Nelonen.fi-29861820.mp4"
    assert req.start == NOW and req.stop == NOW + timedelta(minutes=95)
    assert tv.parse_request(_task(quality=None, keep_days=None)).quality == "low", "low is the default"


def test_a_delete_task_carries_only_its_kind_and_id():
    req = tv.parse_request(
        {"id": "t", "scope": [{"name": "kind", "value": "tvopas.delete"}, {"name": "recording_id", "value": "a.b-1"}]}
    )
    assert (req.kind, req.recording_id) == ("tvopas.delete", "a.b-1")


@pytest.mark.parametrize(
    "over,says",
    [
        ({"kind": "something.else"}, "other than the TV-opas app"),
        ({"kind": None}, "missing"),
        ({"recording_id": "../../etc/passwd"}, "letters, digits"),
        ({"recording_id": "has space"}, "letters, digits"),
        ({"recording_id": ".."}, "letters, digits"),
        ({"stream_url": ""}, "lacks stream_url"),
        ({"quality": "ultra"}, "one of low, medium, high"),
        ({"keep_days": "forever"}, "whole number of days"),
        ({"start": "tomorrow"}, "`start` is not a time"),
        ({"stop": _z(NOW)}, "is not after `start`"),
    ],
)
def test_a_task_that_cannot_be_read_says_what_is_wrong(over, says):
    with pytest.raises(sr.RecordError) as e:
        tv.parse_request(_task(**over))
    assert e.value.code == "BAD_TASK" and says in e.value.message


def test_a_task_written_as_a_sentence_is_refused_not_interpreted(node):
    out = tv.handle_task(AGENT, {"id": "t-9", "title": "Record the evening news please", "description": "tonight"})
    assert out.startswith("BAD_TASK: ") and lifecycle.take_outcome("t-9") == (True, out)


# ── one recording, from the task to the catalogue ───────────────────────────────────────────────


def test_a_recording_is_stored_catalogued_and_closed_with_one_readable_line(node, recorded, tmp_path):
    line = tv.handle_task(AGENT, _task())
    assert line == "Petolliset, Nelonen, 95 min, 248 MB"
    assert lifecycle.take_outcome("t-1") == (False, line)
    assert node.stages() == ["recording", "packing", "uploading"]
    assert node.files == {"tvopas/rec/FI-Nelonen.fi-29861820.mp4": 248_000_000}
    doc = node.memory["tvopas.recordings"]
    assert node.visibility["tvopas.recordings"] == "private"
    assert (doc["v"], doc["storedBytes"], doc["updatedAt"]) == (1, 248_000_000, _z(NOW))
    assert doc["items"] == [
        {
            "id": "FI-Nelonen.fi-29861820",
            "taskId": "t-1",
            "title": "Petolliset",
            "channel": "Nelonen",
            "start": "2026-10-11T18:00:00Z",
            "stop": "2026-10-11T19:35:00Z",
            "quality": "low",
            "width": 640,
            "height": 360,
            "seconds": 5700,
            "bytes": 248_000_000,
            "file": "tvopas/rec/FI-Nelonen.fi-29861820.mp4",
            "recordedAt": "2026-10-11T18:00:00Z",
            "keepUntil": "2026-10-25T18:00:00Z",
        }
    ]
    ask = recorded[0]
    assert ask["seconds"] == 95 * 60 and ask["max_bytes"] == node.limit and ask["user_agent"] == "Mozilla/5.0 test"
    assert not (tmp_path / "work" / "FI-Nelonen.fi-29861820").exists(), "the local files are deleted"


def test_keep_days_zero_keeps_until_asked(node, recorded):
    tv.handle_task(AGENT, _task(keep_days="0"))
    assert "keepUntil" not in node.memory["tvopas.recordings"]["items"][0]


def test_recording_the_same_id_again_replaces_its_item(node, recorded):
    tv.handle_task(AGENT, _task())
    tv.handle_task(AGENT, _task(tid="t-2", title="Petolliset (uusinta)"))
    items = node.memory["tvopas.recordings"]["items"]
    assert [(i["taskId"], i["title"]) for i in items] == [("t-2", "Petolliset (uusinta)")]


def test_a_start_ahead_is_waited_for_and_said(node, recorded, monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(tv, "_now", lambda: clock["t"])
    monkeypatch.setattr(tv.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + timedelta(seconds=max(s, 1))))
    start = NOW + timedelta(minutes=3)
    tv.handle_task(AGENT, _task(start=_z(start), stop=_z(start + timedelta(minutes=30))))
    assert node.stages() == ["waiting", "recording", "packing", "uploading"]
    assert node.events[0] == "waiting until 18:03 UTC to record Petolliset, Nelonen"
    assert recorded[0]["seconds"] == pytest.approx(30 * 60, abs=6)


def test_a_start_within_a_minute_means_now(node, recorded):
    tv.handle_task(AGENT, _task(start=_z(NOW + timedelta(seconds=40))))
    assert node.stages()[0] == "recording"
    assert recorded[0]["seconds"] == 95 * 60, "it records to `stop` from now"


def test_a_task_reached_after_the_start_says_the_beginning_is_missing(node, recorded):
    """The app made it late, or the fleet restarted mid-recording and this is the second go."""
    line = tv.handle_task(AGENT, _task(start=_z(NOW - timedelta(minutes=40)), stop=_z(NOW + timedelta(minutes=55))))
    assert recorded[0]["seconds"] == 55 * 60, "it records what is left, to `stop`"
    assert line.endswith("; the first 40 min are missing: the recorder got to it after the start")
    assert lifecycle.take_outcome("t-1") == (False, line), "what is there is kept, and the task says what is not"


def test_a_task_reached_after_the_programme_ended_is_too_late(node, recorded):
    out = tv.handle_task(AGENT, _task(start=_z(NOW - timedelta(hours=3)), stop=_z(NOW - timedelta(hours=1))))
    assert out.startswith("TOO_LATE: ") and not recorded and not node.files


def test_a_programme_that_cannot_fit_is_refused_before_the_wait(node, recorded):
    node.limit = 10 * 2**20
    start = NOW + timedelta(hours=5)
    out = tv.handle_task(AGENT, _task(start=_z(start), stop=_z(start + timedelta(hours=4))))
    assert out.startswith("TOO_LONG: ") and not node.events and not recorded


def test_a_task_ended_by_its_owner_while_waiting_records_nothing(node, recorded, monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(tv, "_now", lambda: clock["t"])
    monkeypatch.setattr(tv.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + timedelta(seconds=5)))
    monkeypatch.setattr(tv, "LOOK_S", 0.0)
    node.status = "failed"
    out = tv.handle_task(AGENT, _task(start=_z(NOW + timedelta(hours=2)), stop=_z(NOW + timedelta(hours=3))))
    assert out.startswith("CANCELLED: ") and not recorded and node.stages() == ["waiting"]


def test_a_long_stage_says_where_it_is_before_the_node_calls_it_stalled(node, monkeypatch):
    """The node marks a task stalled after 120 minutes without an event; a film says "recording" once."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(tv.time, "monotonic", lambda: clock["t"])
    stages = tv._Stages(AGENT, "t-1")
    stages.say("recording", "Petolliset, Nelonen until 19:35 UTC")
    for _ in range(95):  # a 95-minute film, looked at once a minute
        clock["t"] += 60
        assert stages.ended() is False
    assert node.events == [
        "recording Petolliset, Nelonen until 19:35 UTC",
        "recording Petolliset, Nelonen until 19:35 UTC (30 min so far)",
        "recording Petolliset, Nelonen until 19:35 UTC (60 min so far)",
        "recording Petolliset, Nelonen until 19:35 UTC (90 min so far)",
    ]
    assert tv.STILL_HERE_S < 120 * 60


def test_a_stream_attached_again_is_said_on_the_task_but_not_every_time(node, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(tv.time, "monotonic", lambda: clock["t"])
    stages = tv._Stages(AGENT, "t-1")
    stages.say("recording", "Petolliset, Nelonen until 19:35 UTC")
    for _ in range(4):  # a stream that flaps: four drops inside two minutes
        clock["t"] += 30
        stages.note("the stream dropped after 12 min; trying again with 83 min left")
    clock["t"] += tv.NOTE_S
    stages.note("the stream dropped after 20 min; trying again with 70 min left")
    assert node.events[1:] == [
        "recording Petolliset, Nelonen until 19:35 UTC: the stream dropped after 12 min; trying again with 83 min left",
        "recording Petolliset, Nelonen until 19:35 UTC: the stream dropped after 20 min; trying again with 70 min left",
    ]


def test_a_per_wake_worker_refuses_a_recording_it_would_be_stopped_in(node, recorded, monkeypatch):
    monkeypatch.setenv("AIMEAT_SPAWN_WORKER", "1")
    out = tv.handle_task(AGENT, _task())
    assert out.startswith("RUN_LIMIT: ") and "always on" in out and not recorded
    assert (
        tv.handle_task(AGENT, _task(tid="t-2", stop=_z(NOW + timedelta(minutes=20))))
        == "Petolliset, Nelonen, 95 min, 248 MB"
    )


# ── failures close the task as failed, code first, and leave nothing behind ─────────────────────


def test_a_stream_that_answers_404_fails_the_task_and_leaves_no_file(node, monkeypatch, tmp_path):
    def unreachable(url, seconds, workdir, **kw):
        kw["on_stage"]("recording")
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "part-001.log").write_text("x", "utf-8")
        raise sr.RecordError("STREAM_UNREACHABLE", f"the stream's address answered HTTP 404: {url}")

    monkeypatch.setattr(sr, "record", unreachable)
    out = tv.handle_task(AGENT, _task())
    assert out.startswith("STREAM_UNREACHABLE: the stream's address answered HTTP 404")
    assert lifecycle.take_outcome("t-1") == (True, out)
    assert not node.files and "tvopas.recordings" not in node.memory
    assert not (tmp_path / "work" / "FI-Nelonen.fi-29861820").exists()


def test_a_recording_the_catalogue_would_not_take_is_removed_again(node, recorded):
    node.answers["aimeat_memory_write"] = None
    out = tv.handle_task(AGENT, _task())
    assert out.startswith("UPLOAD_FAILED: ") and "catalogue" in out
    assert not node.files, "stored and not listed is a file nobody would ever delete"


def test_an_unread_catalogue_is_never_written_back_as_an_empty_one(node, recorded):
    node.memory["tvopas.recordings"] = {"v": 1, "items": [{"id": "old", "bytes": 5, "file": "tvopas/rec/old.mp4"}]}
    node.answers["aimeat_memory_read"] = None  # no answer: not the same as "nothing there"
    out = tv.handle_task(AGENT, _task())
    assert out.startswith("UPLOAD_FAILED: the node did not answer the read of the catalogue")
    assert [i["id"] for i in node.memory["tvopas.recordings"]["items"]] == ["old"]
    assert not node.files


def test_a_catalogue_that_is_not_there_yet_is_empty(node):
    assert tv.read_catalogue(AGENT) == {"v": 1, "items": []}


def test_a_stream_that_ended_early_says_how_much_is_there(node, monkeypatch):
    def early(url, seconds, workdir, **kw):
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / "recording.mp4"
        out.write_bytes(b"x" * 1000)
        rec = sr.Recording(out, 1000, 1200, 640, 360, "low", 300, 48, int(seconds), ended_early=True)
        rec.notes.append("the stream ended early: 20 min of 95 min is there")
        return rec

    monkeypatch.setattr(sr, "record", early)
    line = tv.handle_task(AGENT, _task())
    assert line == "Petolliset, Nelonen, 20 min, 0.0 MB; the stream ended early: 20 min of 95 min is there"
    assert lifecycle.take_outcome("t-1") == (False, line), "what was caught is kept and the task completes"


# ── forgetting ──────────────────────────────────────────────────────────────────────────────────


def _delete(rid: str, tid="t-d") -> dict:
    return {
        "id": tid,
        "scope": [
            {"name": "kind", "value": "tvopas.delete", "type": "text"},
            {"name": "recording_id", "value": rid, "type": "text"},
        ],
    }


def test_a_delete_task_leaves_no_file_and_no_item(node, recorded):
    tv.handle_task(AGENT, _task())
    out = tv.handle_task(AGENT, _delete("FI-Nelonen.fi-29861820"))
    assert out == "Deleted Petolliset (248 MB)." and lifecycle.take_outcome("t-d") == (False, out)
    assert node.files == {} and node.memory["tvopas.recordings"]["items"] == []
    assert node.memory["tvopas.recordings"]["storedBytes"] == 0


def test_deleting_what_is_not_there_is_done_already(node):
    assert tv.handle_task(AGENT, _delete("never-was")) == "Nothing to delete: there is no recording never-was."


def test_a_file_left_by_a_run_that_stopped_before_the_catalogue_is_deleted_by_name(node):
    node.files["tvopas/rec/orphan.mp4"] = 7
    tv.handle_task(AGENT, _delete("orphan"))
    assert node.files == {}


def test_expired_recordings_leave_on_every_run_before_anything_else(node, recorded):
    node.files.update({"tvopas/rec/old.mp4": 100, "tvopas/rec/kept.mp4": 200, "tvopas/rec/forever.mp4": 300})
    node.memory["tvopas.recordings"] = {
        "v": 1,
        "items": [
            {"id": "old", "bytes": 100, "file": "tvopas/rec/old.mp4", "keepUntil": _z(NOW - timedelta(seconds=1))},
            {"id": "kept", "bytes": 200, "file": "tvopas/rec/kept.mp4", "keepUntil": _z(NOW + timedelta(days=1))},
            {"id": "forever", "bytes": 300, "file": "tvopas/rec/forever.mp4"},
        ],
    }
    tv.handle_task(AGENT, _task())
    assert sorted(node.files) == [
        "tvopas/rec/FI-Nelonen.fi-29861820.mp4",
        "tvopas/rec/forever.mp4",
        "tvopas/rec/kept.mp4",
    ]
    doc = node.memory["tvopas.recordings"]
    assert [i["id"] for i in doc["items"]] == ["kept", "forever", "FI-Nelonen.fi-29861820"]
    assert doc["storedBytes"] == 200 + 300 + 248_000_000


def test_a_file_the_node_would_not_delete_stays_listed_to_be_tried_again(node):
    node.memory["tvopas.recordings"] = {
        "v": 1,
        "items": [{"id": "old", "bytes": 1, "file": "tvopas/rec/old.mp4", "keepUntil": _z(NOW - timedelta(days=1))}],
    }
    node.answers["aimeat_storage_delete"] = {"ok": False, "error": {"code": "SCOPE_DENIED", "message": "storage:write"}}
    assert tv.purge_expired(AGENT) == []
    assert [i["id"] for i in node.memory["tvopas.recordings"]["items"]] == ["old"]


# ── the completion carries the run's own words ──────────────────────────────────────────────────


class _Calls:
    def __init__(self, answers=None):
        self.calls: list[tuple[str, dict]] = []
        self.answers = answers or {}

    def __call__(self, agent, tool, payload, **kw):
        self.calls.append((tool, payload))
        return self.answers.get(tool, {"ok": True})


def _complete(call, tid="t-1"):
    lc = LifecycleCallbacks(
        call=call,
        eval_ctx=lambda i: None,
        mark_todos_done=lambda a, t: None,
        deliverable_keys={},
        refusals=lambda a, s: [],
    )
    return lc.complete_callback(
        AGENT, tid, mem_key="crews.tv-opas-recorder.t-1.latest_output", since="2026-10-11T18:00:00.000Z"
    )


@pytest.fixture
def outcomes():
    lifecycle._OUTCOMES.clear()
    yield
    lifecycle._OUTCOMES.clear()


def test_a_finished_run_closes_the_task_with_its_own_line(outcomes):
    lifecycle.note_outcome("t-1", "Petolliset, Nelonen, 95 min, 248 MB")
    call = _Calls()
    _complete(call)(None)
    tool, payload = call.calls[-1]
    assert tool == "aimeat_task_complete" and payload["message"] == "Petolliset, Nelonen, 95 min, 248 MB"
    assert payload["deliverable_key"] == "crews.tv-opas-recorder.t-1.latest_output"
    assert lifecycle.take_outcome("t-1") is None, "taken by the completion, not kept for the next task"


def test_a_run_that_says_it_failed_fails_the_task_in_its_own_words(outcomes):
    reason = "STREAM_UNREACHABLE: the stream's address answered HTTP 404: https://x/y.m3u8"
    lifecycle.note_outcome("t-1", reason, failed=True)
    call = _Calls()
    _complete(call)(None)
    assert [t for t, _ in call.calls] == ["aimeat_task_fail"]
    assert call.calls[0][1] == {"task_id": "t-1", "message": reason}


def test_a_fail_the_node_does_not_take_is_said_and_not_raised(outcomes, capsys):
    lifecycle.note_outcome("t-1", "CANCELLED: the task was ended before the recording began.", failed=True)
    _complete(_Calls({"aimeat_task_fail": None}))(None)
    assert "NOT accepted" in capsys.readouterr().err


def test_a_run_that_says_nothing_completes_as_before(outcomes):
    call = _Calls()
    _complete(call)(None)
    assert call.calls[-1][1]["message"].startswith("Crew finished; deliverable published to memory at ")


# ── the upload ──────────────────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.fixture
def upload(monkeypatch, tmp_path):
    """The node's storage doors: every request the upload makes, and the answers it is given."""
    f = tmp_path / "recording.mp4"
    f.write_bytes(b"0123456789" * 3)
    seen: list[tuple[str, str]] = []
    answers: dict[str, _Resp] = {
        "POST /v1/storage": _Resp(
            200, {"ok": True, "data": {"upload_url": "http://node/v1/upload/u_1", "max_size_bytes": 1 << 30}}
        ),
        "PUT upload": _Resp(200, {"success": True, "key": "k.mp4", "size": 30}),
    }

    def request(agent, method, path, **kw):
        seen.append((method, path))
        if "/upload/" in path and method == "PUT":
            return answers.get("PUT chunk", _Resp(200, {"ok": True, "data": {"received": True}}))
        return answers[f"{method} {path if not path.endswith('/complete') else '/complete'}"]

    def put(url, data=None, **kw):
        seen.append(("PUT", url))
        r = answers["PUT upload"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(sr, "_request", request)
    monkeypatch.setattr("requests.put", put)
    return f, seen, answers


def test_one_presigned_put_stores_the_one_file_for_its_owner_only(upload, monkeypatch):
    f, seen, answers = upload
    asked = {}
    real = sr._request
    monkeypatch.setattr(
        sr,
        "_request",
        lambda agent, method, path, **kw: asked.update(kw.get("json") or {}) or real(agent, method, path, **kw),
    )
    out = sr.store_file(AGENT, f, "tvopas/rec/a.mp4")
    assert (out["bytes"], out["route"]) == (30, "put")
    assert asked == {"key": "tvopas/rec/a.mp4", "mime_type": "video/mp4", "visibility": "owner", "mode": "presigned"}
    assert seen == [("POST", "/v1/storage"), ("PUT", "http://node/v1/upload/u_1")]


def test_the_nodes_quota_refusal_says_how_much_is_stored(upload):
    f, seen, answers = upload
    answers["PUT upload"] = _Resp(
        413, {"success": False, "error": "QUOTA_EXCEEDED", "message": "Storage quota exceeded (130.00MB / 100MB)."}
    )
    answers["GET /v1/storage"] = _Resp(
        200, {"ok": True, "data": {"files": [{"key": "a", "size": 60_000_000}, {"key": "b", "size": 40_000_000}]}}
    )
    with pytest.raises(sr.RecordError) as e:
        sr.store_file(AGENT, f, "k.mp4")
    assert e.value.code == "QUOTA" and "Storage quota exceeded" in e.value.message
    assert "already stores 100 MB in 2 file(s)" in e.value.message
    assert not any("/upload/init" in p for _, p in seen), "the node's own refusal is final, not retried in parts"


def test_a_proxy_that_refuses_the_body_gets_the_same_file_in_parts(upload):
    import requests

    f, seen, answers = upload
    answers["POST /v1/storage/upload/init"] = _Resp(
        201, {"ok": True, "data": {"upload_id": "upload-abc", "chunk_size": 8 << 20}}
    )
    answers["POST /complete"] = _Resp(201, {"ok": True, "data": {"key": "k.mp4", "size": 30, "chunks_assembled": 1}})
    for refusal in (_Resp(413, None, "<html>413 Request Entity Too Large</html>"), requests.ConnectionError("reset")):
        seen.clear()
        answers["PUT upload"] = refusal
        out = sr.store_file(AGENT, f, "k.mp4")
        assert out["route"] == "chunked"
        assert [p for _, p in seen][-3:] == [
            "/v1/storage/upload/init",
            "/v1/storage/upload/upload-abc/0",
            "/v1/storage/upload/upload-abc/complete",
        ]


def test_a_file_the_node_stored_short_is_not_called_stored(upload):
    f, seen, answers = upload
    answers["PUT upload"] = _Resp(200, {"success": True, "size": 12})
    with pytest.raises(sr.RecordError) as e:
        sr.store_file(AGENT, f, "k.mp4")
    assert e.value.code == "UPLOAD_FAILED" and "not whole" in e.value.message


def test_a_node_that_gives_no_upload_address_is_named(upload):
    f, seen, answers = upload
    answers["POST /v1/storage"] = _Resp(
        403, {"ok": False, "error": {"code": "SCOPE_DENIED", "message": "storage:write required"}}
    )
    with pytest.raises(sr.RecordError) as e:
        sr.open_upload(AGENT, "k.mp4")
    assert e.value.code == "UPLOAD_FAILED" and "HTTP 403 SCOPE_DENIED: storage:write required" in e.value.message


# ── the declaration, and the tool as a crew definition names it ─────────────────────────────────


def _crew():
    # Parked (`_tv_opas...`) until its owner registers the agent, then renamed: the same file either way.
    (path,) = (_ROOT / "crews").glob("*tv_opas_recorder_crew.py")
    spec = importlib.util.spec_from_file_location("tv_opas_recorder_crew", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_agent_is_what_the_app_looks_for(monkeypatch):
    mod = _crew()
    assert mod.AGENT_NAME == "tv-opas-recorder" and "role:tvopas-recorder" in mod.TAGS
    assert mod.RUN_MODE == "resident", "a per-wake worker is stopped after an hour; a film is longer"
    assert all(len(o["ask"]) <= 500 for o in mod.OFFERS), "the node refuses the whole offers document over 500"
    captured = {}
    monkeypatch.setattr(mod, "run_crew", lambda spec: captured.update(spec=spec))
    mod.run()
    spec = captured["spec"]
    assert callable(spec.on_task) and spec.max_concurrent_tasks == mod.MAX_CONCURRENT > 1
    seen = {}
    monkeypatch.setattr(mod, "handle_task", lambda agent, task: seen.update(agent=agent, task=task) or "done")
    assert spec.on_task({"id": "t"}) == "done" and seen == {"agent": "tv-opas-recorder", "task": {"id": "t"}}


@pytest.mark.parametrize("name", ["..", ".", "", "a/b", "..\\x", ".hidden"])
def test_the_folder_that_is_deleted_afterwards_is_never_named_on_trust(name):
    with pytest.raises(ValueError):
        sr.workdir_for(name)


def test_stream_record_is_a_tool_a_crew_definition_can_name():
    from crewaimeat import crew_def

    assert "stream_record" in crew_def.TOOL_REGISTRY and "stream_record" in crew_def.TOOL_PURPOSES
    tools = crew_def.TOOL_REGISTRY["stream_record"]("some-agent", None)
    assert [t.name for t in tools] == ["stream_record"]


def test_the_tool_answers_a_failure_with_its_code_first(monkeypatch, tmp_path):
    monkeypatch.setattr(sr, "workdir_for", lambda name: tmp_path / name)
    monkeypatch.setattr(sr, "open_upload", lambda agent, key, **k: {"upload_url": "u", "max_size_bytes": 1 << 30})

    def gone(*a, **k):
        raise sr.RecordError("STREAM_UNREACHABLE", "the stream's address answered HTTP 404: https://x/y.m3u8")

    monkeypatch.setattr(sr, "record", gone)
    (tool,) = sr.make_stream_record_tools("some-agent")
    assert (
        tool.run(url="https://x/y.m3u8", seconds=60)
        == "stream_record stopped: STREAM_UNREACHABLE: the stream's address answered HTTP 404: https://x/y.m3u8"
    )
    assert tool.run(url="https://x/y.m3u8", seconds=60, storage_key="../up.mp4").startswith(
        "stream_record stopped: BAD_REQUEST: "
    )
