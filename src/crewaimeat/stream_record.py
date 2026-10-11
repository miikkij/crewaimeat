"""stream_record — record a live HLS stream for a given time and compress it into ONE MP4.

WHY (wish-videontallennus-omalle-aimeat-nodelle-tallennus-ajastettu-na, brief doc-mv2t4n84hsf3). The
TV-opas app plays live streams; the owner wants to record a programme and watch it later. A node
extension cannot do that: it has no ffmpeg, no hour of wall clock and no disk. The owner's ruling of
2026-10-10 is that such work goes to an agent, and that a recording is ONE MP4 file, never a set of
segments. So this tool does the bytes, the way `file_fetch` does for big files:

1. CAPTURE with stream copy from now for `seconds`. Nothing is decoded, so the capture costs no CPU
   and drops nothing. A master playlist is read first and the smallest picture that still covers the
   asked quality is taken, so a 360p recording does not download the 1080p rendition. A live stream
   that drops is re-attached until the clock runs out; what each attachment caught is one part.
2. PACK afterwards into one MP4: H.264 + AAC, the index at the front (`+faststart`), so a browser
   plays it from the first bytes and a seek is one range request. The picture is never upscaled and
   the frame rate is kept up to 25.
3. FIT. The node says how large one file may be. The size is worked out from the length and the
   bitrate before compressing, and the video bitrate is lowered until nine tenths of the limit holds
   it; below 200 kbit/s the recording is refused as TOO_LONG instead of made unwatchable.
4. STORE the one file in the agent's OWN storage, visible to its owner only.

Every failure is a `RecordError` with a code a program can read and a sentence a person can.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse


@dataclass(frozen=True)
class Quality:
    name: str
    height: int
    video_kbps: int
    audio_kbps: int


#: The owner's three qualities (brief doc-mv2t4n84hsf3). `low` is the default: small size matters
#: more than picture quality, and a small file is a kindness to the node that holds it in memory.
QUALITIES: dict[str, Quality] = {
    q.name: q
    for q in (
        Quality("low", 360, 300, 48),
        Quality("medium", 480, 630, 64),
        Quality("high", 720, 1400, 96),
    )
}
DEFAULT_QUALITY = "low"
#: The brief's floor: a recording that cannot be made to fit at this video bitrate is TOO_LONG.
MIN_VIDEO_KBPS = 200
#: The brief's ceiling on the frame rate; a slower source keeps its own.
MAX_FPS = 25
#: The brief: aim at nine tenths of the node's limit for one file.
FIT_FRACTION = 0.9
#: What the MP4 container itself costs: a table entry per frame and per audio packet. Measured on a
#: ten-minute recording at `low` (25 fps video, AAC sound): 5.9 kbit/s. Rounded up, because this is
#: the estimate a recording is fitted by, and it is the brief's "one hour is about 160 MB" as well.
MUX_KBPS = 8
#: The brief: a stream that ended early is kept when more than a minute of it was caught.
MIN_KEEP_S = 60.0
_ALLOW_ENV = "STREAM_RECORD_ALLOW_HOSTS"
_MB = 1000 * 1000  # a recording's size the way a person reads it (248 000 000 bytes is 248 MB)
_MIB = 1024 * 1024  # the node's limit the way the node counts it (its setting is in these)
#: What ffmpeg may open while it follows a playlist. No `file`: a playlist is somebody else's text,
#: and it does not get to name a path on this machine.
_PROTOCOLS = "http,https,tcp,tls,crypto,data,httpproxy"
_PLAYLIST_BYTES = 1 << 20
#: How long past its asked length a capture may run before it is ended: a stream that stalls delivers
#: no time for ffmpeg to count, so the wall clock is what ends it.
_STALL_GRACE_S = 60.0
#: The brief's part size for the node's chunked upload route.
_CHUNK_BYTES = 8 * _MIB
#: A name this tool will use for a file or a folder: letters, digits, dot, dash and underscore, and
#: never starting with a dot, so `..` is not a name.
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
#: A storage key this tool will write: such names, separated by `/`.
_KEY = re.compile(rf"{_NAME}(/{_NAME})*")


class RecordError(Exception):
    """A recording that could not be made. `code` is for the program, the message for the person."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


@dataclass
class Recording:
    """One finished MP4 on this machine's disk, and what is true of it."""

    path: Path
    bytes: int
    seconds: int
    width: int
    height: int
    quality: str
    video_kbps: int
    audio_kbps: int
    requested_seconds: int
    lowered: bool = False  # the video bitrate was lowered so the file fits the node
    ended_early: bool = False  # the stream gave less than was asked
    parts: int = 1  # how many attachments the capture took
    notes: list[str] = field(default_factory=list)


def span(seconds: float) -> str:
    """A length as a person says it: 95 min, 2 h 40 min, 45 s."""
    s = int(round(seconds))
    if s < 90:
        return f"{s} s"
    m = int(round(s / 60))
    if m < 120:
        return f"{m} min"
    return f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h"


def megabytes(n: float) -> str:
    return f"{n / _MB:.0f} MB" if n >= 10 * _MB else f"{n / _MB:.1f} MB"


def _limit(max_bytes: int) -> str:
    return f"{max_bytes / _MIB:g} MB"


# --------------------------------------------------------------------------- ffmpeg


def find_ffmpeg() -> tuple[str, str]:
    """(ffmpeg, ffprobe) on this machine, or NO_FFMPEG. Installing it is the owner's decision."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        missing = " and ".join(n for n, p in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not p)
        raise RecordError(
            "NO_FFMPEG",
            f"{missing} is not installed on the computer that runs this agent, and a recording cannot be "
            f"made without it. Its owner installs ffmpeg there; then the recording can be asked again.",
        )
    return ffmpeg, ffprobe


def _run_quiet(cmd: list[str], *, low_priority: bool = False, **kw) -> subprocess.Popen:
    """Start ffmpeg. Compression runs below normal priority: it takes every core for minutes, and the
    machine it runs on is a fleet host with other agents' work on it."""
    if low_priority and os.name == "nt":
        kw["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
    elif low_priority:
        kw["preexec_fn"] = lambda: os.nice(10)  # noqa: PLW1509 -- one call before exec, nothing shared
    if os.name != "nt":
        kw["start_new_session"] = True  # its own process group, so `_end` reaches whatever it starts
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, **kw)  # noqa: S603


def _end(proc: subprocess.Popen) -> None:
    """End ffmpeg AND whatever it started. On Windows the `ffmpeg.exe` a package manager puts on the
    path is often a shim that starts the real one as its child (Chocolatey's is; measured on the fleet
    host), and ending the shim alone leaves the real ffmpeg recording with nobody waiting for it."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(  # noqa: S603
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],  # noqa: S607 -- the same call spawner._terminate_pid makes
            capture_output=True,
            timeout=30,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=15)


def _stop(proc: subprocess.Popen) -> None:
    """Ask ffmpeg to finish its file (`q`), and end it when it does not."""
    with contextlib.suppress(OSError, ValueError):
        proc.stdin.write(b"q")
        proc.stdin.flush()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _end(proc)


def _tail(log: Path, lines: int = 4) -> str:
    try:
        text = log.read_text("utf-8", errors="replace")
    except OSError:
        return ""
    return " | ".join(ln.strip() for ln in text.strip().splitlines()[-lines:] if ln.strip())[:400]


def media_info(ffprobe: str, path: Path) -> dict:
    """{seconds, width, height, fps, video, audio} of a media file, from ffprobe."""
    out = subprocess.run(  # noqa: S603
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    try:
        doc = json.loads(out.stdout or "{}")
    except ValueError:
        doc = {}
    info: dict[str, Any] = {"seconds": 0.0, "width": 0, "height": 0, "fps": 0.0, "video": "", "audio": ""}
    with contextlib.suppress(TypeError, ValueError):
        info["seconds"] = float((doc.get("format") or {}).get("duration") or 0.0)
    for s in doc.get("streams") or []:
        kind = s.get("codec_type")
        if kind == "video" and not info["video"]:
            info.update(
                video=s.get("codec_name") or "?", width=int(s.get("width") or 0), height=int(s.get("height") or 0)
            )
            info["fps"] = _rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate"))
        elif kind == "audio" and not info["audio"]:
            info["audio"] = s.get("codec_name") or "?"
    return info


def _rate(text: Any) -> float:
    num, _, den = str(text or "").partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


# --------------------------------------------------------------------------- the stream


def _allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in re.split(r"[,\s]+", os.getenv(_ALLOW_ENV) or "") if h.strip()}


def check_stream_url(url: str) -> None:
    """Refuse an address that is not a public http(s) one. A stream address arrives in a task, and a
    task does not get to point this machine at itself or its own network; the owner allows such a
    host by name (STREAM_RECORD_ALLOW_HOSTS). Plain http is taken: most public streams are http."""
    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host:
        raise RecordError("STREAM_UNREACHABLE", f"{url!r} is not an http(s) address.")
    if host in _allowed_hosts():
        return
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RecordError("STREAM_UNREACHABLE", f"the stream's host {host} could not be found ({exc}).") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise RecordError(
                "STREAM_UNREACHABLE",
                f"{host} is an address inside this computer or its network ({ip}); only its owner can "
                f"allow such a host ({_ALLOW_ENV}).",
            )


def _get_playlist(url: str, headers: dict) -> str:
    """The text at `url`, or the failure that names the HTTP status."""
    import requests

    try:
        with requests.get(url, headers=headers, timeout=(15, 30), stream=True) as r:
            status = r.status_code
            body = r.raw.read(_PLAYLIST_BYTES, decode_content=True) if status == 200 else b""
    except requests.RequestException as exc:
        raise RecordError(
            "STREAM_UNREACHABLE", f"the stream's address did not answer ({type(exc).__name__}): {url}"
        ) from exc
    if status in (403, 451):
        raise RecordError(
            "GEO_BLOCKED",
            f"the stream's host refused this computer (HTTP {status}). That is nearly always a channel "
            f"that plays only inside its own country, and sometimes a missing user agent or referrer.",
        )
    if status != 200:
        raise RecordError("STREAM_UNREACHABLE", f"the stream's address answered HTTP {status}: {url}")
    return body.decode("utf-8", errors="replace")


_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _variants(text: str, base: str) -> list[dict]:
    """The renditions a master playlist lists, in its own order: [{index, height, bandwidth, url}].
    The order is what ffmpeg numbers its programs by, so `index` selects one there."""
    out: list[dict] = []
    lines = [ln.strip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = {k: v.strip('"') for k, v in _ATTR.findall(line.split(":", 1)[1])}
        uri = next((ln for ln in lines[i + 1 :] if ln and not ln.startswith("#")), "")
        height = 0
        m = re.fullmatch(r"\d+x(\d+)", attrs.get("RESOLUTION", ""))
        if m:
            height = int(m.group(1))
        bandwidth = int(attrs["BANDWIDTH"]) if attrs.get("BANDWIDTH", "").isdigit() else 0
        out.append({"index": len(out), "height": height, "bandwidth": bandwidth, "url": urljoin(base, uri)})
    return out


def pick_variant(variants: list[dict], want_height: int) -> dict | None:
    """The smallest picture that is at least the asked height; when none is, the largest there is.
    None when the playlist does not say how tall its renditions are (ffmpeg then takes the best)."""
    sized = [v for v in variants if v["height"] > 0]
    if not sized:
        return None
    enough = [v for v in sized if v["height"] >= want_height]
    if enough:
        return min(enough, key=lambda v: (v["height"], v["bandwidth"]))
    return max(sized, key=lambda v: (v["height"], v["bandwidth"]))


def look_at_stream(url: str, want_height: int, *, user_agent: str = "", referrer: str = "") -> dict:
    """Read the playlist once before recording: is the stream there, which rendition to take, and is
    it live. Returns {program, live, picked}. A failure here is the one that names an HTTP status."""
    check_stream_url(url)
    headers = {"User-Agent": user_agent or "Mozilla/5.0 (crewaimeat stream_record)"}
    if referrer:
        headers["Referer"] = referrer
    text = _get_playlist(url, headers)
    if "#EXTM3U" not in text[:2048]:
        # Not a playlist (a bare transport stream, say). ffmpeg decides what it is; nothing to pick.
        return {"program": None, "live": False, "picked": None}
    picked = pick_variant(_variants(text, url), want_height)
    media = text
    if picked is not None:
        media = _get_playlist(picked["url"], headers)
    elif "#EXT-X-STREAM-INF:" in text:
        return {"program": None, "live": True, "picked": None}
    return {
        "program": picked["index"] if picked else None,
        "live": "#EXT-X-ENDLIST" not in media,
        "picked": picked,
    }


def _slack(seconds: float) -> float:
    """How much less than asked still counts as all of it: a stream is cut at its own segments."""
    return max(15.0, seconds * 0.02)


def _capture_cmd(ffmpeg: str, url: str, seconds: float, out: Path, seen: dict, ua: str, ref: str) -> list:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-y", "-protocol_whitelist", _PROTOCOLS]
    if ua:
        cmd += ["-user_agent", ua]
    if ref:
        cmd += ["-referer", ref]
    if seen["live"]:
        # A player starts three segments back to have something buffered. A recorder has no use for
        # the buffer, and every segment back is that much of the programme's end it will not reach.
        cmd += ["-live_start_index", "-1"]
    cmd += ["-rw_timeout", "30000000", "-i", url]
    program = seen["program"]
    where = f"0:p:{program}:" if program is not None else "0:"
    cmd += ["-map", f"{where}v:0?", "-map", f"{where}a:0?"]
    # MPEG-TS for the capture: a file cut off in the middle is still a file, which an MP4 is not.
    return [*cmd, "-t", f"{seconds:.1f}", "-c", "copy", "-f", "mpegts", str(out)]


def _capture_once(cmd: list, log: Path, seconds: float, should_stop: Callable[[], bool] | None) -> None:
    """Run one attachment. Ends when ffmpeg has its `seconds`, when the wall clock is a minute past
    them (a stream that stalls delivers no time to count), or when the caller says stop."""
    give_up = time.monotonic() + seconds + _STALL_GRACE_S
    with open(log, "wb") as err:
        proc = _run_quiet(cmd, stderr=err)
        try:
            while proc.poll() is None:
                if time.monotonic() > give_up or (should_stop is not None and should_stop()):
                    _stop(proc)
                    break
                time.sleep(1.0)
        finally:
            _end(proc)


def _why_nothing(log_tail: str) -> RecordError:
    """Name the failure of a stream that answered with a playlist and then gave no picture."""
    m = re.search(r"\b(?:HTTP error|Server returned) (\d{3})", log_tail)
    if m and m.group(1) in ("403", "451"):
        return RecordError(
            "GEO_BLOCKED",
            f"the stream's host refused this computer (HTTP {m.group(1)}) when the picture was asked for. "
            f"That is nearly always a channel that plays only inside its own country.",
        )
    if m:
        return RecordError(
            "STREAM_UNREACHABLE", f"the stream answered HTTP {m.group(1)} when the picture was asked for."
        )
    return RecordError("STREAM_ENDED_EARLY", f"the stream gave nothing to record. ffmpeg said: {log_tail or 'nothing'}")


def capture(
    url: str,
    seconds: float,
    workdir: Path,
    *,
    want_height: int,
    user_agent: str = "",
    referrer: str = "",
    should_stop: Callable[[], bool] | None = None,
    on_note: Callable[[str], None] | None = None,
) -> tuple[list[Path], float]:
    """Record `url` from now for `seconds`, stream copy. Returns (the parts in order, seconds caught).

    A live stream that drops, or gives nothing yet, is attached again for as long as the clock has
    time left: the clock is the only limit, because it is the one the person set, and `on_note` hears
    each time it happens. Measured 2026-10-11 on a real channel: one attachment in twenty-one gave
    nothing for a moment, and failing there would have cost the whole programme. Two things are not
    tried again. A host that REFUSES the picture (an HTTP status in what ffmpeg said) answers the same
    way a second time, so that fails at once with its status. And a stream that is not live (its
    playlist ends) is read once; attaching again would record the same minutes twice."""
    ffmpeg, ffprobe = find_ffmpeg()
    seen = look_at_stream(url, want_height, user_agent=user_agent, referrer=referrer)
    end = time.monotonic() + seconds
    parts: list[Path] = []
    nothing: RecordError | None = None
    caught, pause, ask, attempt = 0.0, 3.0, float(seconds), 0
    while True:
        attempt += 1
        part, log = workdir / f"part-{attempt:03d}.ts", workdir / f"part-{attempt:03d}.log"
        _capture_once(_capture_cmd(ffmpeg, url, ask, part, seen, user_agent, referrer), log, ask, should_stop)
        got = media_info(ffprobe, part)["seconds"] if part.exists() and part.stat().st_size > 0 else 0.0
        if got > 0:
            parts.append(part)
            caught += got
            pause = 3.0
        else:
            nothing = _why_nothing(_tail(log))
            if not parts and nothing.code != "STREAM_ENDED_EARLY":
                raise nothing
        if (should_stop is not None and should_stop()) or not seen["live"] or got >= ask - _slack(ask):
            break
        left = end - time.monotonic()
        if left < 5:
            break
        note = f"the stream dropped after {span(caught)}" if parts else "the stream gives no picture yet"
        note += f"; trying again with {span(left)} left"
        print(f"[stream_record] {note} ({_tail(log, 1) or 'no message'})", file=sys.stderr)
        if on_note:
            on_note(note)
        time.sleep(min(pause, left))
        pause = min(pause * 2, 30.0)
        ask = end - time.monotonic()  # the recording still ends when the person said, however long the gap was
        if ask < 5:
            break
    if not parts:
        raise nothing or RecordError("STREAM_ENDED_EARLY", "the stream gave nothing to record.")
    return parts, caught


# --------------------------------------------------------------------------- fit and pack


def tier_for(asked: Quality, source_height: int) -> Quality:
    """The bitrates that belong to the picture actually produced. A 360p source asked at `high` stays
    360p (never upscaled), and a 360p picture is paid for at 360p's bitrate, not at 720p's."""
    if source_height <= 0 or source_height >= asked.height:
        return asked
    for q in sorted(QUALITIES.values(), key=lambda q: q.height):
        if q.height >= source_height:
            return q if q.height <= asked.height else asked
    return asked


def predicted_bytes(seconds: float, video_kbps: float, audio_kbps: float) -> int:
    return int((video_kbps + audio_kbps + MUX_KBPS) * 1000 / 8 * seconds)


def fit_video_kbps(seconds: float, q: Quality, max_bytes: int | None) -> tuple[int, bool]:
    """(video kbit/s, lowered?) so that `seconds` at `q` fits nine tenths of `max_bytes`; TOO_LONG
    when even the floor does not. No limit known: the quality's own bitrate."""
    if not max_bytes or seconds <= 0:
        return q.video_kbps, False
    room = max_bytes * FIT_FRACTION * 8 / 1000 / seconds - q.audio_kbps - MUX_KBPS
    if room >= q.video_kbps:
        return q.video_kbps, False
    if room < MIN_VIDEO_KBPS:
        longest = max_bytes * FIT_FRACTION * 8 / 1000 / (MIN_VIDEO_KBPS + q.audio_kbps + MUX_KBPS)
        raise RecordError(
            "TOO_LONG",
            f"{span(seconds)} does not fit this node's limit of {_limit(max_bytes)} for one file, even "
            f"at {MIN_VIDEO_KBPS} kbit/s. The longest recording that fits is {span(longest)}.",
        )
    return int(room), True


def check_fits(seconds: float, max_bytes: int | None) -> None:
    """TOO_LONG when `seconds` cannot fit the node's limit even at the floor. Asked before a minute is
    recorded (and before a wait of hours), with the smallest sound: the picture may turn out small
    enough to be paid at that tier, and what is refused here must be what could never have fitted."""
    lowest = QUALITIES[DEFAULT_QUALITY]
    fit_video_kbps(seconds, Quality(lowest.name, lowest.height, MIN_VIDEO_KBPS, lowest.audio_kbps), max_bytes)


def _pack_cmd(ffmpeg: str, parts: list[Path], out: Path, q: Quality, video_kbps: int, src: dict) -> list:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-y"]
    if len(parts) == 1:
        cmd += ["-i", str(parts[0])]
    else:
        listing = out.with_suffix(".parts.txt")
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), "utf-8")
        cmd += ["-f", "concat", "-safe", "0", "-i", str(listing)]
    if src["video"]:
        # -2 keeps the width even and the shape right; a source at or under the asked height is not scaled up.
        scale = f"scale=-2:{q.height}" if src["height"] > q.height else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        vf = scale + (f",fps={MAX_FPS}" if src["fps"] > MAX_FPS + 0.01 else "")
        cmd += ["-map", "0:v:0", "-vf", vf, "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
        cmd += ["-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.5)}k", "-bufsize", f"{video_kbps * 3}k"]
    if src["audio"]:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", f"{q.audio_kbps}k", "-ac", "2"]
    # The index at the front: a browser plays from the first bytes and a seek is one range request.
    return [*cmd, "-movflags", "+faststart", "-f", "mp4", str(out)]


def pack(parts: list[Path], out: Path, q: Quality, video_kbps: int, src: dict) -> None:
    """Compress the captured parts into ONE MP4 at `out`."""
    ffmpeg, _ = find_ffmpeg()
    log = out.with_suffix(".pack.log")
    with open(log, "wb") as err:
        proc = _run_quiet(_pack_cmd(ffmpeg, parts, out, q, video_kbps, src), low_priority=True, stderr=err)
        code = proc.wait()
    if code != 0 or not out.exists() or out.stat().st_size == 0:
        raise RecordError("PACK_FAILED", f"the recording could not be compressed. ffmpeg said: {_tail(log) or code}")


def record(
    url: str,
    seconds: float,
    workdir: Path,
    *,
    quality: str = DEFAULT_QUALITY,
    max_bytes: int | None = None,
    user_agent: str = "",
    referrer: str = "",
    min_keep_s: float = MIN_KEEP_S,
    should_stop: Callable[[], bool] | None = None,
    on_stage: Callable[[str], None] | None = None,
    on_note: Callable[[str], None] | None = None,
) -> Recording:
    """Record `url` from now for `seconds` and leave ONE MP4 in `workdir`. Raises RecordError.

    `max_bytes` is the node's limit for one file; with it the recording is fitted (see the module
    text). `on_stage` hears "recording" and "packing" as each begins, and `on_note` hears each time
    the stream had to be attached again. `should_stop` is asked about once a second during the capture; when it says yes the capture ends and the recording is given up
    (CANCELLED), because a recording somebody stopped is not one they want half of.
    """
    q = QUALITIES.get(str(quality or DEFAULT_QUALITY).lower())
    if q is None:
        raise RecordError("BAD_REQUEST", f"quality must be one of {list(QUALITIES)}, not {quality!r}.")
    if seconds <= 0:
        raise RecordError("BAD_REQUEST", "there is no time to record: the recording's end is not after its start.")
    _, ffprobe = find_ffmpeg()
    check_fits(seconds, max_bytes)
    workdir.mkdir(parents=True, exist_ok=True)

    if on_stage:
        on_stage("recording")
    parts, caught = capture(
        url,
        seconds,
        workdir,
        want_height=q.height,
        user_agent=user_agent,
        referrer=referrer,
        should_stop=should_stop,
        on_note=on_note,
    )
    if should_stop is not None and should_stop():
        raise RecordError("CANCELLED", f"the recording was stopped after {span(caught)} of {span(seconds)}.")
    notes: list[str] = []
    ended_early = caught < seconds - _slack(seconds)
    if ended_early and caught < min_keep_s:
        raise RecordError(
            "STREAM_ENDED_EARLY",
            f"the stream ended after {span(caught)} of {span(seconds)}, which is too little to keep.",
        )
    if ended_early:
        notes.append(f"the stream ended early: {span(caught)} of {span(seconds)} is there")

    src = media_info(ffprobe, parts[0])
    if not src["video"] and not src["audio"]:
        raise RecordError("STREAM_ENDED_EARLY", "what the stream gave holds no picture and no sound.")
    tier = tier_for(q, src["height"])
    video_kbps, lowered = fit_video_kbps(caught, tier, max_bytes)
    if lowered:
        notes.append(f"quality lowered to fit: {span(caught)} at {video_kbps} kbit/s")

    if on_stage:
        on_stage("packing")
    out = workdir / "recording.mp4"
    pack(parts, out, tier, video_kbps, src)
    if max_bytes and out.stat().st_size > max_bytes:
        # The encoder overshot its average. One more pass, lowered by exactly what it overshot.
        again = int(video_kbps * max_bytes * FIT_FRACTION / out.stat().st_size)
        if again < MIN_VIDEO_KBPS:
            raise RecordError(
                "TOO_LONG",
                f"{span(caught)} came to {megabytes(out.stat().st_size)} at {video_kbps} kbit/s, over this "
                f"node's limit of {_limit(max_bytes)} for one file, and it cannot go lower.",
            )
        video_kbps, lowered = again, True
        notes[:] = [n for n in notes if not n.startswith("quality lowered")]
        notes.append(f"quality lowered to fit: {span(caught)} at {video_kbps} kbit/s")
        pack(parts, out, tier, video_kbps, src)
        if out.stat().st_size > max_bytes:
            raise RecordError(
                "TOO_LONG",
                f"{span(caught)} came to {megabytes(out.stat().st_size)}, over this node's limit of "
                f"{_limit(max_bytes)} for one file.",
            )
    done = media_info(ffprobe, out)
    for p in parts:  # the capture is the large half of the disk this used; it goes as soon as the MP4 is whole
        with contextlib.suppress(OSError):
            p.unlink()
    rec = Recording(
        path=out,
        bytes=out.stat().st_size,
        seconds=int(round(done["seconds"] or caught)),
        width=done["width"],
        height=done["height"],
        quality=q.name,
        video_kbps=video_kbps if src["video"] else 0,
        audio_kbps=tier.audio_kbps if src["audio"] else 0,
        requested_seconds=int(round(seconds)),
        lowered=lowered,
        ended_early=ended_early,
        parts=len(parts),
        notes=notes,
    )
    print(
        f"[stream_record] {url} -> {out.name}: {span(rec.seconds)} of {span(seconds)} asked, {rec.width}x{rec.height}, "
        f"{rec.video_kbps}+{rec.audio_kbps} kbit/s, {megabytes(rec.bytes)}, {rec.parts} part(s)"
        + (f"; {'; '.join(notes)}" if notes else ""),
        file=sys.stderr,
    )
    return rec


# --------------------------------------------------------------------------- the node's storage


def _request(agent: str, method: str, path: str, **kw):
    from crewaimeat.aimeat_crew import _aimeat_request

    return _aimeat_request(agent, method, path, **kw)


def _answer(r) -> tuple[dict, str, str]:
    """(data, error code, error message) of a node answer, whichever of its two shapes it came in."""
    try:
        body = r.json()
    except ValueError:
        return {}, "", (r.text or "")[:200]
    if not isinstance(body, dict):
        return {}, "", str(body)[:200]
    err = body.get("error")
    if isinstance(err, dict):  # the envelope: {ok, data, error: {code, message}}
        return body.get("data") or {}, str(err.get("code") or ""), str(err.get("message") or "")
    if err:  # the upload door: {success: false, error: CODE, message}
        return {}, str(err), str(body.get("message") or "")
    return (body.get("data") if isinstance(body.get("data"), dict) else body), "", ""


def recording_provenance(mime: str = "video/mp4") -> dict:
    """What a recording is, for the node's provenance record: `original`, no model involved.

    WHY IT IS SAID. An agent's write with no declaration is stamped as model-written, and the file is
    then served with `AI-Disclosure: mode=machine-generated`: a television programme labelled as
    generated (measured 2026-10-11 on the first recordings). No model produced or changed any of it,
    so the upload says so (owner's ruling, same day). The declaration needs the scope
    `provenance:write`; without it the node refuses the whole upload with SCOPE_DENIED and names the
    scope, which is the failure the task then carries."""
    from aimeat_crewai.provenance import Level, declare

    return {
        **declare(
            Level.ORIGINAL,
            notes="A recording of a broadcast stream, compressed by ffmpeg. No model produced or changed it.",
        ),
        "media_kind": mime.split("/", 1)[0] if mime.split("/", 1)[0] in ("video", "audio") else "data",
        "media_type": mime,
    }


def open_upload(agent: str, key: str, *, mime: str = "video/mp4", visibility: str = "owner") -> dict:
    """Ask the node for an address to upload `key` to. The answer also says how large one file may be
    (`max_size_bytes`), which is how a caller asks the node for its limit instead of assuming one.
    The upload is declared as a recording (`recording_provenance`)."""
    body = {"key": key, "mime_type": mime, "visibility": visibility, "mode": "presigned"}
    body["ai_provenance"] = recording_provenance(mime)
    try:
        r = _request(agent, "POST", "/v1/storage", json=body, timeout=60)
    except Exception as exc:  # noqa: BLE001 -- whatever stopped the question is the reason the person reads
        raise RecordError(
            "UPLOAD_FAILED", f"the node could not be asked for an upload address ({type(exc).__name__}: {exc})"
        ) from exc
    data, code, message = _answer(r)
    if r.status_code != 200 or not data.get("upload_url"):
        raise RecordError(
            "UPLOAD_FAILED",
            f"the node did not give an upload address (HTTP {r.status_code} {code}: {message or 'no reason given'}).",
        )
    return data


def stored_files(agent: str) -> tuple[int, int] | None:
    """(files, bytes) this agent has in its storage, as the node counts them; None when it will not say."""
    try:
        r = _request(agent, "GET", "/v1/storage", timeout=60)
    except Exception:  # noqa: BLE001 -- this is the colour of a failure message, never its cause
        return None
    data, _, _ = _answer(r)
    files = data.get("files")
    if r.status_code != 200 or not isinstance(files, list):
        return None
    return len(files), sum(int(f.get("size") or 0) for f in files if isinstance(f, dict))


def _refused(agent: str, status: int, code: str, message: str) -> RecordError:
    """The node's answer to an upload it did not take, as the failure the task carries."""
    if code == "QUOTA_EXCEEDED":
        have = stored_files(agent)
        held = f" This agent already stores {megabytes(have[1])} in {have[0]} file(s)." if have else ""
        return RecordError("QUOTA", f"the node refused the upload: {message}{held}")
    return RecordError("UPLOAD_FAILED", f"the node did not take the file (HTTP {status} {code}: {message}).")


def _store_chunked(agent: str, path: Path, key: str, mime: str, visibility: str) -> dict:
    """The same one file, sent in several requests: for a node behind a proxy that refuses one large body."""
    size = path.stat().st_size
    chunks = max(1, -(-size // _CHUNK_BYTES))
    init = {"key": key, "mime_type": mime, "visibility": visibility, "chunk_size": _CHUNK_BYTES, "total_chunks": chunks}
    r = _request(agent, "POST", "/v1/storage/upload/init", json=init, timeout=60)
    data, code, message = _answer(r)
    upload_id = data.get("upload_id")
    if r.status_code not in (200, 201) or not upload_id:
        raise _refused(agent, r.status_code, code, message)
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for i in range(chunks):
                block = fh.read(_CHUNK_BYTES)
                digest.update(block)
                # Straight to the node with the agent's own credential: the loopback tunnel carries text.
                r = _request(
                    agent,
                    "PUT",
                    f"/v1/storage/upload/{upload_id}/{i}",
                    data=block,
                    headers={"Content-Type": "application/octet-stream"},
                    direct=True,
                    retries=3,
                    timeout=300,
                )
                if r.status_code != 200:
                    _, code, message = _answer(r)
                    raise _refused(agent, r.status_code, code, f"part {i + 1} of {chunks}: {message}")
        r = _request(
            agent,
            "POST",
            f"/v1/storage/upload/{upload_id}/complete",
            json={"checksum_sha256": digest.hexdigest(), "ai_provenance": recording_provenance(mime)},
            timeout=900,
        )
        data, code, message = _answer(r)
        if r.status_code not in (200, 201):
            raise _refused(agent, r.status_code, code, message)
    except BaseException:
        # The node keeps the parts of an unfinished upload in its memory for six hours. An upload that
        # is not going to finish gives them back now.
        with contextlib.suppress(Exception):
            _request(agent, "DELETE", f"/v1/storage/upload/{upload_id}", timeout=30)
        raise
    return data


def store_file(agent: str, path: Path, key: str, *, mime: str = "video/mp4", visibility: str = "owner") -> dict:
    """Upload ONE file into the agent's OWN storage at `key`. Returns {bytes, seconds, route}.

    One presigned PUT first. A proxy in front of a node may refuse a body of hundreds of MB (413, or a
    cut connection) before the node sees it; then the same file goes by the chunked route. A refusal
    by the NODE (its limit, the agent's quota) is final and is not tried again another way."""
    import requests

    size = path.stat().st_size
    started = time.monotonic()
    slot = open_upload(agent, key, mime=mime, visibility=visibility)
    data: dict = {}
    route, via_proxy = "put", ""
    try:
        with open(path, "rb") as fh:
            put = requests.put(slot["upload_url"], data=fh, headers={"Content-Type": mime}, timeout=(30, 900))
    except requests.RequestException as exc:
        via_proxy = f"the connection was cut ({type(exc).__name__})"
    else:
        data, code, message = _answer(put)
        if put.status_code == 413 and not code:
            via_proxy = "HTTP 413 from something in front of the node"
        elif put.status_code not in (200, 201) or code:
            raise _refused(agent, put.status_code, code, message)
    if via_proxy:
        print(
            f"[stream_record] {agent}: the single upload of {megabytes(size)} did not get through ({via_proxy}); "
            f"sending it in {megabytes(_CHUNK_BYTES)} parts",
            file=sys.stderr,
        )
        try:
            data = _store_chunked(agent, path, key, mime, visibility)
        except RecordError:
            raise
        except Exception as exc:  # noqa: BLE001 -- both roads failed; the person reads both reasons
            raise RecordError(
                "UPLOAD_FAILED",
                f"the file did not reach the node: the single upload failed ({via_proxy}) and so did the "
                f"upload in parts ({type(exc).__name__}: {exc}).",
            ) from exc
        route = "chunked"
    if int(data.get("size") or size) != size:
        raise RecordError(
            "UPLOAD_FAILED", f"the node stored {data.get('size')} bytes of a file that has {size}; it is not whole."
        )
    return {"bytes": size, "seconds": round(time.monotonic() - started, 1), "route": route}


def delete_file(agent: str, key: str) -> bool:
    """Remove `key` from the agent's own storage. True when it is gone, which includes never having been there."""
    from crewaimeat.aimeat_crew import _aimeat_call

    r = _aimeat_call(agent, "aimeat_storage_delete", {"key": key}, return_error=True, quiet=True)
    if isinstance(r, dict) and r.get("ok") is False:
        err = r.get("error")
        code = str((err.get("code") if isinstance(err, dict) else err) or "")
        if code == "NOT_FOUND" or r.get("http_status") == 404:
            return True
        print(f"[stream_record] {agent}: delete {key} refused: {err}", file=sys.stderr)
        return False
    return r is not None


def workdir_for(name: str) -> Path:
    """Where one recording's files live while it is made: under the connector home, which is not in git."""
    from crewaimeat._home import aimeat_home

    if not re.fullmatch(_NAME, name or ""):
        # This folder is deleted when the recording is done, so its name is never taken on trust:
        # `..` here would be the connector home itself, tokens and all.
        raise ValueError(f"{name!r} is not a name for a recording's folder")
    return aimeat_home() / "stream_record" / name


# --------------------------------------------------------------------------- the runtime tool


def make_stream_record_tools(agent_name: str) -> list:
    """The crew_def `stream_record` tool, bound to `agent_name` (whose own storage the recording goes to)."""
    from crewai.tools import tool

    @tool("stream_record")
    def stream_record(
        url: str,
        seconds: int,
        quality: str = DEFAULT_QUALITY,
        storage_key: str = "",
        user_agent: str = "",
        referrer: str = "",
    ) -> str:
        """Record a live video stream (an HLS .m3u8 address) from NOW for `seconds`, compress it into
        ONE MP4 a browser plays (H.264 + AAC), and store it in your own storage on the node, readable
        by your owner only. The call returns when the recording is stored, so it takes `seconds` plus
        the time to compress. You get back the storage key, the size and the length.

        url: the stream's http(s) address.
        seconds: how long to record.
        quality: "low" (360p, about 160 MB an hour; the default), "medium" (480p, about 320 MB) or
          "high" (720p, about 680 MB). The picture is never made larger than the stream's own. A
          recording too long for the node's file limit is recorded at a lower bitrate, and the answer
          says so; one that cannot fit at all is refused as TOO_LONG.
        storage_key: where to store it, e.g. "recordings/evening-news.mp4" (letters, digits, . - _ and
          /). Left empty, a key is made from the time.
        user_agent, referrer: sent to the stream's host when it asks for them.
        A failure starts with its code: STREAM_UNREACHABLE (with the HTTP status), GEO_BLOCKED,
        STREAM_ENDED_EARLY, NO_FFMPEG, TOO_LONG, QUOTA, UPLOAD_FAILED."""
        key = (storage_key or "").strip() or f"recordings/{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.mp4"
        work = workdir_for(uuid.uuid4().hex[:12])
        try:
            if not _KEY.fullmatch(key):
                raise RecordError("BAD_REQUEST", f"storage_key {key!r} may hold letters, digits, . - _ and / only.")
            limit = int(open_upload(agent_name, key).get("max_size_bytes") or 0) or None
            rec = record(
                url, float(seconds), work, quality=quality, max_bytes=limit, user_agent=user_agent, referrer=referrer
            )
            sent = store_file(agent_name, rec.path, key)
        except RecordError as exc:
            print(f"[stream_record] {agent_name}: {url} stopped: {exc}", file=sys.stderr)
            return f"stream_record stopped: {exc}"
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return json.dumps(
            {
                "key": key,
                "bytes": rec.bytes,
                "seconds": rec.seconds,
                "width": rec.width,
                "height": rec.height,
                "quality": rec.quality,
                "video_kbps": rec.video_kbps,
                "upload_seconds": sent["seconds"],
                "notes": rec.notes,
            },
            ensure_ascii=False,
        )

    return [stream_record]
