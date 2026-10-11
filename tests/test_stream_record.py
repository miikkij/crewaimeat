"""stream_record: capture an HLS stream with stream copy, then compress it into ONE fitted MP4.

The stream is a real one: ffmpeg writes a two-rendition HLS ladder into a temp folder and a loopback
server this test starts serves it (the host allowed through STREAM_RECORD_ALLOW_HOSTS, the way an owner
would allow one). So the playlist read, the rendition choice, the capture, the compression and the
index-at-the-front are all the real code paths. Skipped on a machine without ffmpeg, which is exactly
the machine where the tool answers NO_FFMPEG.
"""

from __future__ import annotations

import functools
import http.server
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import crewaimeat.stream_record as sr

_HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def needs_ffmpeg(test):
    """A test that runs the real ffmpeg against the loopback stream; skipped where there is none."""
    for mark in (
        pytest.mark.ffmpeg,
        pytest.mark.loopback,
        pytest.mark.skipif(not _HAS_FFMPEG, reason="no ffmpeg here"),
    ):
        test = mark(test)
    return test


CLIP_S = 12


def _ladder(root: Path) -> None:
    """A 12-second stream in two renditions, 720p listed FIRST so choosing by position would be wrong."""
    for name, size in (("v720", "1280x720"), ("v360", "640x360")):
        (root / name).mkdir()
        subprocess.run(  # noqa: S603
            [
                shutil.which("ffmpeg"),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={size}:rate=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440",
                "-t",
                str(CLIP_S),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-g",
                "30",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-f",
                "hls",
                "-hls_time",
                "2",
                "-hls_playlist_type",
                "vod",
                "-hls_segment_filename",
                str(root / name / "seg%03d.ts"),
                str(root / name / "index.m3u8"),
            ],
            check=True,
        )
    # The same 360p stream as a LIVE one that has stalled: a playlist with no end marker that never
    # grows. ffmpeg takes what is there and then waits for segments that do not come.
    listed = (root / "v360" / "index.m3u8").read_text("utf-8").split("#EXT-X-ENDLIST")[0]
    listed = listed.replace("#EXT-X-PLAYLIST-TYPE:VOD\n", "")
    (root / "v360" / "stalled.m3u8").write_text(listed, "utf-8")
    (root / "master.m3u8").write_text(
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2"\n'
        "v720/index.m3u8\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=700000,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2"\n'
        "v360/index.m3u8\n",
        "utf-8",
    )


@pytest.fixture(scope="module")
def stream(tmp_path_factory):
    """The base URL of a loopback server serving the ladder, plus /gone (404) and /abroad (403)."""
    root = tmp_path_factory.mktemp("hls")
    _ladder(root)
    hits: dict[str, int] = {}

    class H(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            flaky = re.fullmatch(r"/flaky-(\w+)(/.*)", self.path)
            if flaky:
                # A host that cuts its first connections, as live-fi.tvkaista.net did on 2026-10-11: the
                # first request for the playlist is answered with nothing in it, the second is dropped
                # without an answer, and from the third on the stream is there.
                self.path = flaky.group(2)
                if self.path.endswith(".m3u8"):
                    hits[flaky.group(1)] = hits.get(flaky.group(1), 0) + 1
                    if hits[flaky.group(1)] == 1:
                        self.send_response(200)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if hits[flaky.group(1)] == 2:
                        self.close_connection = True
                        return
            if self.path.startswith("/gone"):
                self.send_error(404)
            elif self.path.startswith("/abroad"):
                self.send_error(403)
            else:
                super().do_GET()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setenv("STREAM_RECORD_ALLOW_HOSTS", "127.0.0.1")


def _probe(path: Path) -> dict:
    return sr.media_info(shutil.which("ffprobe"), path)


# ── the arithmetic, with no ffmpeg ──────────────────────────────────────────────────────────────


def test_an_hour_at_low_is_the_size_the_brief_says():
    q = sr.QUALITIES["low"]
    assert 155e6 < sr.predicted_bytes(3600, q.video_kbps, q.audio_kbps) < 165e6


def test_the_bitrate_is_lowered_until_nine_tenths_of_the_limit_holds_it():
    low = sr.QUALITIES["low"]
    assert sr.fit_video_kbps(600, low, 30 * 2**20) == (300, False)
    kbps, lowered = sr.fit_video_kbps(600, low, 24 * 2**20)
    assert lowered and 200 <= kbps < 300
    assert sr.predicted_bytes(600, kbps, low.audio_kbps) <= 0.9 * 24 * 2**20
    assert sr.fit_video_kbps(600, low, None) == (300, False), "no limit known: the quality's own bitrate"


def test_a_recording_that_does_not_fit_at_the_floor_is_too_long():
    with pytest.raises(sr.RecordError) as e:
        sr.fit_video_kbps(600, sr.QUALITIES["low"], 20 * 2**20)
    assert e.value.code == "TOO_LONG" and "longest recording that fits" in e.value.message


def test_a_small_picture_is_paid_at_its_own_tier():
    q = sr.QUALITIES
    assert sr.tier_for(q["high"], 360) is q["low"]
    assert sr.tier_for(q["high"], 480) is q["medium"]
    assert sr.tier_for(q["high"], 1080) is q["high"]
    assert sr.tier_for(q["low"], 240) is q["low"]
    assert sr.tier_for(q["medium"], 0) is q["medium"], "an unknown height changes nothing"


def test_the_rendition_taken_is_the_smallest_that_covers_the_quality():
    ladder = [
        {"index": 0, "height": 1080, "bandwidth": 5_000_000, "url": "a"},
        {"index": 1, "height": 360, "bandwidth": 700_000, "url": "b"},
        {"index": 2, "height": 720, "bandwidth": 2_500_000, "url": "c"},
    ]
    assert sr.pick_variant(ladder, 360)["index"] == 1
    assert sr.pick_variant(ladder, 480)["index"] == 2
    assert sr.pick_variant(ladder[1:2], 720)["index"] == 1, "nothing tall enough: the tallest there is"
    assert sr.pick_variant([{"index": 0, "height": 0, "bandwidth": 1, "url": "a"}], 360) is None


def test_lengths_read_the_way_a_person_says_them():
    assert sr.span(45) == "45 s" and sr.span(5700) == "95 min" and sr.span(9600) == "2 h 40 min"
    assert sr.span(7200) == "2 h"


def test_an_address_inside_the_machine_is_refused_unless_the_owner_allows_it(monkeypatch):
    monkeypatch.delenv("STREAM_RECORD_ALLOW_HOSTS", raising=False)
    with pytest.raises(sr.RecordError) as e:
        sr.check_stream_url("http://127.0.0.1:9/x.m3u8")
    assert e.value.code == "STREAM_UNREACHABLE" and "STREAM_RECORD_ALLOW_HOSTS" in e.value.message
    with pytest.raises(sr.RecordError):
        sr.check_stream_url("file:///etc/passwd")
    monkeypatch.setenv("STREAM_RECORD_ALLOW_HOSTS", "127.0.0.1")
    sr.check_stream_url("http://127.0.0.1:9/x.m3u8")


def test_no_ffmpeg_is_said_before_anything_is_tried(monkeypatch, tmp_path):
    monkeypatch.setattr(sr.shutil, "which", lambda name: None)
    with pytest.raises(sr.RecordError) as e:
        sr.record("https://example.org/a.m3u8", 60, tmp_path)
    assert e.value.code == "NO_FFMPEG" and str(e.value).startswith("NO_FFMPEG: ")


def test_a_length_that_cannot_fit_is_refused_before_a_minute_is_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(sr, "find_ffmpeg", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(sr, "capture", lambda *a, **k: pytest.fail("recorded a programme that could not fit"))
    with pytest.raises(sr.RecordError) as e:
        sr.record("https://example.org/a.m3u8", 4 * 3600, tmp_path, max_bytes=10 * 2**20)
    assert e.value.code == "TOO_LONG"


# ── trying again: a stream that does not open, and a live one that drops ────────────────────────

LIVE = {"program": None, "live": True, "picked": None}


def _scripted(monkeypatch, tmp_path, lengths: list[float], said: str = "Connection reset by peer", looks=None):
    """Stand in for ffmpeg and for the look at the playlist, on a clock the test owns. Each attachment
    'catches' the next length in `lengths`; each look answers the next of `looks` (a dict, or a
    RecordError to raise; the last one repeats; none given, the stream is live and there)."""
    asked = _Asked()
    clock = asked.clock = {"t": 0.0, "looks": 0}
    monkeypatch.setattr(sr, "find_ffmpeg", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(sr.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(sr.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    def look(*a, **k):
        clock["looks"] += 1
        answer = (looks or [LIVE])[min(clock["looks"], len(looks or [LIVE])) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def once(cmd, log, seconds, should_stop):
        asked.append(seconds)
        got = lengths[len(asked) - 1]
        clock["t"] += got
        Path(cmd[-1]).write_bytes(b"x" * int(got))
        Path(log).write_text(said, "utf-8")

    monkeypatch.setattr(sr, "look_at_stream", look)
    monkeypatch.setattr(sr, "_capture_once", once)
    monkeypatch.setattr(sr, "media_info", lambda ffprobe, path: {"seconds": float(path.stat().st_size)})
    return asked


class _Asked(list):
    """What each attachment was asked for, with the test's clock: `t` seconds gone, `looks` made."""

    clock: dict


def _clock(asked) -> dict:
    return asked.clock


def test_a_live_stream_that_drops_is_attached_again_until_the_clock_runs_out(monkeypatch, tmp_path):
    notes: list[tuple[str, bool]] = []
    asked = _scripted(monkeypatch, tmp_path, [200.0, 0.0, 380.0])
    parts, caught = sr.capture(
        "http://x/live.m3u8", 600, tmp_path, want_height=360, on_note=lambda t, e: notes.append((t, e))
    )
    assert [p.name for p in parts] == ["part-001.ts", "part-003.ts"] and caught == 580.0
    assert asked[0] == 600 and asked[1] < 400, "after a drop it asks for what the clock has left, not the whole length"
    assert asked[2] < asked[1], "the gap is not recorded past the end the person set"
    assert notes[0] == ("the stream dropped after 3 min; trying again with 7 min left", False)
    assert _clock(asked)["looks"] == 1, "a stream that has recorded is not looked at again"


def test_a_stream_that_does_not_open_at_first_is_opened_again_and_recorded(monkeypatch, tmp_path):
    """aimeat.io 2026-10-11, task 576d0225: the host cut one connection, the recorder had allowed itself
    one attempt, and a 25-minute programme failed in two seconds. The same address worked afterwards."""
    notes: list[tuple[str, bool]] = []
    asked = _scripted(monkeypatch, tmp_path, [0.0, 597.0], said="[tls @ 0x1] IO error: End of file")
    parts, caught = sr.capture(
        "http://x/live.m3u8", 600, tmp_path, want_height=360, on_note=lambda t, e: notes.append((t, e))
    )
    assert [p.name for p in parts] == ["part-002.ts"] and caught == 597.0 and len(asked) == 2
    assert notes == [("the stream did not open (attempt 1 of 10); trying again in 3 s", True)]
    assert _clock(asked)["looks"] == 2, "each attempt looks at the stream afresh"


def test_a_look_that_got_no_playlist_does_not_decide_the_stream_is_not_live(monkeypatch, tmp_path):
    """What made the failure final in two seconds: the cut connection's answer was no playlist, a
    stream that is not a playlist counted as not live, and one that is not live got one attempt."""
    not_a_playlist = {"program": None, "live": False, "picked": None}
    asked = _scripted(monkeypatch, tmp_path, [0.0, 600.0], looks=[not_a_playlist, LIVE])
    parts, caught = sr.capture("http://x/live.m3u8", 600, tmp_path, want_height=360)
    assert caught == 600.0 and len(asked) == 2


def test_a_stream_that_never_opens_fails_after_ten_attempts_and_not_before(monkeypatch, tmp_path):
    notes: list[str] = []
    asked = _scripted(monkeypatch, tmp_path, [0.0] * 50, said="[tls @ 0x1] IO error: End of file")
    with pytest.raises(sr.RecordError) as e:
        sr.capture("http://x/live.m3u8", 3600, tmp_path, want_height=360, on_note=lambda t, e: notes.append(t))
    assert e.value.code == "STREAM_ENDED_EARLY"
    assert e.value.message.startswith("after 10 attempts the stream gave nothing to record. ffmpeg said: ")
    assert len(asked) == sr.OPEN_ATTEMPTS == 10 and len(notes) == 9, "one note per wait, none after the last"
    assert [n.split("trying again in ")[1] for n in notes] == ["3 s", "6 s", "12 s", "24 s"] + ["30 s"] * 5
    assert _clock(asked)["t"] == 3 + 6 + 12 + 24 + 30 * 5 == 195, "the waits, on a clock where an attempt takes no time"


def test_a_host_that_answers_with_a_status_keeps_its_code_and_says_how_often_it_was_asked(monkeypatch, tmp_path):
    gone = sr.RecordError("STREAM_UNREACHABLE", "the stream's address answered HTTP 404: http://x/live.m3u8")
    asked = _scripted(monkeypatch, tmp_path, [600.0], looks=[gone])
    with pytest.raises(sr.RecordError) as e:
        sr.capture("http://x/live.m3u8", 3600, tmp_path, want_height=360)
    assert str(e.value) == (
        "STREAM_UNREACHABLE: after 10 attempts the stream's address answered HTTP 404: http://x/live.m3u8"
    )
    assert asked == [] and _clock(asked)["looks"] == 10, "ffmpeg is not started on an address that answered 404"

    abroad = sr.RecordError("GEO_BLOCKED", "the stream's host refused this computer (HTTP 403).")
    _scripted(monkeypatch, tmp_path, [600.0], looks=[abroad])
    with pytest.raises(sr.RecordError) as e:
        sr.capture("http://x/live.m3u8", 3600, tmp_path, want_height=360)
    assert e.value.code == "GEO_BLOCKED" and e.value.message.startswith("after 10 attempts the stream's host refused")


def test_a_status_that_goes_away_is_recorded_after_it(monkeypatch, tmp_path):
    gone = sr.RecordError("STREAM_UNREACHABLE", "the stream's address answered HTTP 404: http://x/live.m3u8")
    asked = _scripted(monkeypatch, tmp_path, [591.0], looks=[gone, gone, LIVE])
    parts, caught = sr.capture("http://x/live.m3u8", 600, tmp_path, want_height=360)
    assert caught == 591.0 and len(asked) == 1 and asked[0] == 600 - 3 - 6


def test_a_window_that_closes_first_ends_the_attempts_and_says_so(monkeypatch, tmp_path):
    asked = _scripted(monkeypatch, tmp_path, [0.0] * 50)
    with pytest.raises(sr.RecordError) as e:
        sr.capture("http://x/live.m3u8", 40, tmp_path, want_height=360)
    assert e.value.code == "STREAM_ENDED_EARLY" and len(asked) == 4
    assert e.value.message.startswith("after 4 attempts, when the recording's time ran out, the stream gave nothing")


def test_an_address_that_is_wrong_in_itself_is_not_asked_again(monkeypatch, tmp_path):
    asked = _scripted(monkeypatch, tmp_path, [600.0], looks=[sr.RecordError("STREAM_UNREACHABLE", "x", retry=False)])
    with pytest.raises(sr.RecordError):
        sr.capture("ftp://x/live.m3u8", 600, tmp_path, want_height=360)
    assert _clock(asked)["looks"] == 1 and _clock(asked)["t"] == 0
    monkeypatch.delenv("STREAM_RECORD_ALLOW_HOSTS", raising=False)
    for bad in ("ftp://example.org/a.m3u8", "http://127.0.0.1:9/a.m3u8"):
        with pytest.raises(sr.RecordError) as e:
            sr.check_stream_url(bad)
        assert e.value.retry is False


def test_a_stop_during_the_wait_between_two_attempts_is_obeyed_within_a_second(monkeypatch, tmp_path):
    """The waits grow to half a minute; a Stop is not made to sit one out."""
    asked = _scripted(monkeypatch, tmp_path, [0.0] * 50)
    clock = _clock(asked)
    stop_at = 3 + 6 + 12 + 24 + 7.5  # in the middle of the fifth wait, which is 30 s long
    parts, caught = sr.capture(
        "http://x/live.m3u8", 3600, tmp_path, want_height=360, should_stop=lambda: clock["t"] >= stop_at
    )
    assert (parts, caught) == ([], 0.0) and len(asked) == 5
    assert stop_at <= clock["t"] <= stop_at + 1.0, "looked for once a second, in the wait as well"

    _scripted(monkeypatch, tmp_path, [0.0] * 50)
    with pytest.raises(sr.RecordError) as e:
        sr.record("http://x/live.m3u8", 3600, tmp_path / "w", should_stop=lambda: sr.time.monotonic() >= 20)
    assert e.value.code == "CANCELLED", "a stop is CANCELLED, never the failure of the attempt before it"


def test_what_ffmpeg_was_refused_names_the_failure():
    assert sr._why_nothing("[https @ 0] HTTP error 403 Forbidden").code == "GEO_BLOCKED"
    assert sr._why_nothing("Server returned 404 Not Found").code == "STREAM_UNREACHABLE"
    assert sr._why_nothing("Invalid data found when processing input").code == "STREAM_ENDED_EARLY"


# ── the real thing ──────────────────────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_six_seconds_at_low_is_one_mp4_a_browser_plays_from_its_first_bytes(stream, allowed, tmp_path):
    stages: list[str] = []
    rec = sr.record(f"{stream}/master.m3u8", 6, tmp_path, quality="low", on_stage=stages.append)
    assert stages == ["recording", "packing"]
    assert rec.path.name == "recording.mp4" and rec.bytes == rec.path.stat().st_size > 0
    info = _probe(rec.path)
    assert (info["video"], info["audio"]) == ("h264", "aac")
    assert (rec.width, rec.height) == (640, 360) == (info["width"], info["height"])
    assert abs(info["fps"] - 25) < 0.1, "a 30 fps source is brought down to 25"
    assert 5 <= rec.seconds <= 8 and not rec.ended_early and not rec.lowered
    head = rec.path.read_bytes()
    assert 0 < head.find(b"moov") < head.find(b"mdat"), "the index must come before the picture"
    assert not list(tmp_path.glob("part-*.ts")), "the capture is deleted once the MP4 is whole"


@needs_ffmpeg
def test_the_360p_rendition_is_the_one_captured_for_low(stream, allowed):
    seen = sr.look_at_stream(f"{stream}/master.m3u8", 360)
    assert seen["program"] == 1 and seen["picked"]["height"] == 360 and seen["live"] is False
    assert sr.look_at_stream(f"{stream}/master.m3u8", 720)["program"] == 0


@needs_ffmpeg
def test_a_small_source_asked_at_high_stays_small(stream, allowed, tmp_path):
    rec = sr.record(f"{stream}/v360/index.m3u8", 4, tmp_path, quality="high")
    assert rec.height == 360 and rec.quality == "high"
    assert rec.video_kbps == sr.QUALITIES["low"].video_kbps, "a 360p picture is paid at 360p's bitrate"


@needs_ffmpeg
def test_a_recording_is_lowered_to_fit_the_nodes_limit(stream, allowed, tmp_path):
    limit = 400_000
    rec = sr.record(f"{stream}/master.m3u8", 10, tmp_path, quality="low", max_bytes=limit)
    assert rec.lowered and 200 <= rec.video_kbps < 300 and rec.bytes <= limit
    assert any(n.startswith("quality lowered to fit: 10 s at ") for n in rec.notes)


@needs_ffmpeg
def test_a_stream_that_ends_early_is_kept_when_enough_was_caught(stream, allowed, tmp_path):
    rec = sr.record(f"{stream}/master.m3u8", 60, tmp_path, quality="low", min_keep_s=5)
    assert rec.ended_early and 10 <= rec.seconds <= 13 and rec.requested_seconds == 60
    assert rec.notes == ["the stream ended early: 12 s of 60 s is there"]


@needs_ffmpeg
def test_a_stream_that_ends_too_early_to_keep_is_failed(stream, allowed, tmp_path):
    with pytest.raises(sr.RecordError) as e:
        sr.record(f"{stream}/master.m3u8", 600, tmp_path, quality="low")
    assert e.value.code == "STREAM_ENDED_EARLY" and "12 s of 10 min" in e.value.message


@needs_ffmpeg
def test_a_stop_during_the_compression_ends_it_there(stream, allowed, tmp_path):
    """A film takes a minute or more to compress; Stop is not made to wait for that."""
    work = tmp_path / "w"
    work.mkdir()
    parts, caught = sr.capture(f"{stream}/v720/index.m3u8", 10, work, want_height=720)
    src = sr.media_info(shutil.which("ffprobe"), parts[0])
    began = time.monotonic()
    with pytest.raises(sr.RecordError) as e:
        sr.pack(parts, work / "recording.mp4", sr.QUALITIES["high"], 1400, src, should_stop=lambda: True)
    assert e.value.code == "CANCELLED" and "compressed" in e.value.message and time.monotonic() - began < 30
    shutil.rmtree(work)  # nothing of ffmpeg still holds a file in it
    assert not work.exists()


@needs_ffmpeg
def test_a_recording_somebody_stopped_ends_ffmpeg_and_keeps_nothing(stream, allowed, tmp_path):
    """The capture is a process of its own, and on Windows often a shim with the real ffmpeg under it.
    Stopped, nothing of it may be left running: a folder whose file a process still holds cannot be deleted."""
    began = time.monotonic()
    with pytest.raises(sr.RecordError) as e:
        sr.record(f"{stream}/v360/stalled.m3u8", 600, tmp_path / "w", should_stop=lambda: time.monotonic() - began > 3)
    assert e.value.code == "CANCELLED" and time.monotonic() - began < 40
    shutil.rmtree(tmp_path / "w")
    assert not (tmp_path / "w").exists()


@needs_ffmpeg
def test_a_live_stream_that_stalls_is_ended_by_the_wall_clock(stream, allowed, tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_STALL_GRACE_S", 2.0)
    began = time.monotonic()
    assert sr.look_at_stream(f"{stream}/v360/stalled.m3u8", 360)["live"] is True
    rec = sr.record(f"{stream}/v360/stalled.m3u8", 4, tmp_path / "w", min_keep_s=1)
    assert time.monotonic() - began < 60, "ffmpeg waits for segments for ever; the clock ends it"
    assert 1 <= rec.seconds <= 13 and rec.path.exists()


@needs_ffmpeg
@pytest.mark.parametrize(
    "path,code,status", [("/gone/a.m3u8", "STREAM_UNREACHABLE", "404"), ("/abroad/a.m3u8", "GEO_BLOCKED", "403")]
)
def test_a_stream_that_does_not_answer_fails_with_its_http_status(
    stream, allowed, tmp_path, path, code, status, monkeypatch
):
    monkeypatch.setattr(sr, "OPEN_WAITS", (0.0,))  # the waits are held by their own tests, on a clock
    with pytest.raises(sr.RecordError) as e:
        sr.record(f"{stream}{path}", 30, tmp_path)
    assert e.value.code == code and f"HTTP {status}" in e.value.message
    assert e.value.message.startswith("after 10 attempts the stream's ")
    assert not list(tmp_path.glob("*.mp4")) and not list(tmp_path.glob("*.ts"))
