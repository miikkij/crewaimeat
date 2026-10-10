"""file_fetch — download a big file, unpack it, pick the parts a job needs, and hand them on, with no
model carrying the bytes.

WHY (wish-crewaimeat-file-fetch-ty-kalu-joka-lataa-purkaa-ja-suodattaa, brief doc-mv1j1f24qasf). Work a
node extension cannot do goes to an agent with an app tool, so the system learns that kind of job. The
first case: TV-opas has streams but no programme guide for the Finnish Rakuten TV channels. The guide is
in a 9.5 MB gzip that inflates to 76 MB, every country together; the Finnish part is about 140 kB. A node
extension's sandbox reads at most 32 MB in 5 s, no tool in the runtime menu downloaded and inflated a
binary file, and a model cannot hold 76 MB -- nor should the records pass through one. So the model
decides WHAT to fetch and WHERE it goes, and this tool does the bytes:

1. DOWNLOAD an https URL, streamed to a temporary file, under a raw byte ceiling and a time ceiling the
   call states. http, and any host that resolves inside this machine or its network, only when the
   owner allows that host (FILE_FETCH_ALLOW_HOSTS) -- an agent can be talked into fetching an address,
   and a loopback or metadata address is not one a prompt gets to choose. Every redirect hop is checked.
2. UNPACK by content, not by name: gzip (1f 8b), zip (PK\\x03\\x04: a named member or the first), or
   none. What comes out is counted against an inflated ceiling, which is what stops a gzip bomb.
3. PICK while streaming, so memory stays flat whatever the file's size: xml elements by tag with an
   attribute test (expat; a finished element is dropped unless it sits inside one being collected),
   lines by regex, json items under a path (ijson), csv rows where a column matches.
4. HAND OVER without the model: `pipe_to` an app tool (the picked sets placed into its input by a small
   map) or a workspace row space, and only the answer and the counts come back; otherwise a summary --
   counts, the first few records -- and a `ref:` the next call reads instead of a URL.

Every call prints one line to stderr naming the source, the sizes, the counts and where the records
went: the run log shows what was handled, and that only counts reached the model.
"""

from __future__ import annotations

import contextlib
import csv
import gzip
import html
import io
import ipaddress
import itertools
import json
import os
import re
import socket
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

#: The owner's stated default raw ceiling (brief doc-mv1j1f24qasf). Every ceiling is the call's to raise.
DEFAULT_MAX_MB = 64.0
DEFAULT_MAX_INFLATED_MB = 1024.0
DEFAULT_TIMEOUT_S = 300.0
#: aimeat_workspace_rows_append takes "up to 500" rows per call (the node's own tool description).
_ROWS_PER_APPEND = 500
_PREVIEW = 3
#: A `ref:` is a scratch copy for the next call or a retry, not a store; older ones are removed.
_REF_KEEP_S = 7 * 24 * 3600
_ALLOW_ENV = "FILE_FETCH_ALLOW_HOSTS"
_FORMATS = ("xml", "lines", "json", "csv")
_TESTS = ("equals", "prefix", "contains", "regex")
_MB = 1024 * 1024


class FetchError(Exception):
    """A refusal or failure, in words the agent can act on."""


class CeilingError(FetchError):
    """A ceiling the call stated was reached; the message names it and how to raise it."""


# --------------------------------------------------------------------------- download


def _allowed_hosts() -> set[str]:
    return {h.strip().lower() for h in re.split(r"[,\s]+", os.getenv(_ALLOW_ENV) or "") if h.strip()}


def _check_url(url: str) -> None:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme not in ("https", "http") or not host:
        raise FetchError(f"{url!r} is not an http(s) URL.")
    allowed = host in _allowed_hosts()
    if p.scheme == "http" and not allowed:
        raise FetchError(
            f"{url} is plain http. file_fetch reads https; http only from a host the owner allows ({_ALLOW_ENV})."
        )
    if allowed:
        return
    try:
        infos = socket.getaddrinfo(host, p.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError(f"{host} could not be resolved: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FetchError(
                f"{host} resolves to {ip}, an address inside this machine or its network. Only the owner can "
                f"allow such a host ({_ALLOW_ENV})."
            )


def _mb(n: float) -> str:
    return f"{n / _MB:.1f} MB"


def _download(url: str, dest, max_bytes: int, deadline: float, timeout_s: float) -> dict:
    """Stream `url` into the open binary file `dest`. Returns {url (after redirects), bytes, content_type}."""
    import requests

    hops = 0
    with requests.Session() as s:
        while True:
            _check_url(url)
            left = deadline - time.monotonic()
            if left <= 0:
                raise CeilingError(f"Stopped at the time ceiling of {timeout_s:g} s (timeout_s) before {url} answered.")
            r = s.get(
                url,
                stream=True,
                allow_redirects=False,
                timeout=(min(30.0, left), min(60.0, left)),
                headers={"User-Agent": "crewaimeat-file_fetch", "Accept-Encoding": "identity"},
            )
            with r:
                if r.is_redirect:
                    hops += 1
                    if hops > requests.models.DEFAULT_REDIRECT_LIMIT:
                        raise FetchError(f"{url} redirected more than {requests.models.DEFAULT_REDIRECT_LIMIT} times.")
                    url = urljoin(url, r.headers.get("Location") or "")
                    continue
                if r.status_code != 200:
                    raise FetchError(f"{url} answered HTTP {r.status_code}.")
                declared = int(r.headers.get("Content-Length") or 0)
                if declared > max_bytes:
                    raise CeilingError(
                        f"Stopped at the download ceiling of {_mb(max_bytes)} (max_mb): {url} declares "
                        f"{_mb(declared)}. Raise max_mb if this file is meant to be that big."
                    )
                n = 0
                for chunk in r.iter_content(1 << 16):
                    n += len(chunk)
                    if n > max_bytes:
                        raise CeilingError(
                            f"Stopped at the download ceiling of {_mb(max_bytes)} (max_mb): {url} sent more. "
                            f"Raise max_mb if this file is meant to be that big."
                        )
                    if time.monotonic() > deadline:
                        raise CeilingError(
                            f"Stopped at the time ceiling of {timeout_s:g} s (timeout_s) after {_mb(n)} of {url}."
                        )
                    dest.write(chunk)
                return {"url": url, "bytes": n, "content_type": r.headers.get("Content-Type") or ""}


# --------------------------------------------------------------------------- unpack


class _Bounded(io.RawIOBase):
    """Counts what is read through it, and stops at the inflated ceiling and at the deadline. Every
    picker reads through one of these, so the two ceilings are enforced in exactly one place."""

    def __init__(self, inner, limit: int, deadline: float, timeout_s: float, max_inflated_mb: float):
        self._inner, self._limit, self._deadline = inner, limit, deadline
        self._timeout_s, self._max_inflated_mb = timeout_s, max_inflated_mb
        self.count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self._inner.read(len(b))
        n = len(data)
        b[:n] = data
        self.count += n
        if self.count > self._limit:
            raise CeilingError(
                f"Stopped at the unpacked ceiling of {self._max_inflated_mb:g} MB (max_inflated_mb): the file "
                f"inflates past it. Raise max_inflated_mb if it is meant to be that big; a few kB that "
                f"inflate to gigabytes is a decompression bomb."
            )
        if time.monotonic() > self._deadline:
            raise CeilingError(
                f"Stopped at the time ceiling of {self._timeout_s:g} s (timeout_s) after unpacking {_mb(self.count)}."
            )
        return n


def _open_payload(path: Path, member: str, stack: contextlib.ExitStack):
    """The decompressed byte stream of `path`, and how it was unpacked."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] == b"\x1f\x8b":
        return stack.enter_context(gzip.open(path, "rb")), "gzip"
    if head == b"PK\x03\x04":
        zf = stack.enter_context(zipfile.ZipFile(path))
        files = [i for i in zf.infolist() if not i.is_dir()]
        if not files:
            raise FetchError("The zip holds no files.")
        info = files[0]
        if member:
            hits = [i for i in files if i.filename == member or i.filename.rsplit("/", 1)[-1] == member]
            if not hits:
                raise FetchError(f"The zip has no member {member!r}. Its members: {[i.filename for i in files]}.")
            info = hits[0]
        return stack.enter_context(zf.open(info)), f"zip:{info.filename}"
    return stack.enter_context(open(path, "rb")), "none"


# --------------------------------------------------------------------------- pick


def _selector(raw: Any, fmt: str, name: str) -> dict:
    if not isinstance(raw, dict):
        raise FetchError(f"pick set {name!r} must be an object.")
    sel = dict(raw)
    if fmt == "xml" and not sel.get("tag"):
        raise FetchError(f'pick set {name!r} needs `tag` (the element name, e.g. "programme").')
    if fmt == "lines" and not (sel.get("regex") or any(k in sel for k in _TESTS)):
        raise FetchError(f"pick set {name!r} needs `regex` (or prefix/contains/equals) to choose lines.")
    if "regex" in sel:
        try:
            sel["_rx"] = re.compile(str(sel["regex"]))
        except re.error as exc:
            raise FetchError(f"pick set {name!r}: regex {sel['regex']!r} does not compile: {exc}") from exc
    return sel


def _parse_pick(pick: Any) -> tuple[str, dict, dict]:
    """(format, {set name: selector}, options) from the call's `pick`."""
    if not isinstance(pick, dict) or not pick:
        raise FetchError(
            'pick is required: {"format": "xml|lines|json|csv", "sets": {"<name>": {<selector>}}} -- '
            'e.g. {"format": "xml", "sets": {"items": {"tag": "item", "attr": "lang", "equals": "fi"}}}.'
        )
    fmt = str(pick.get("format") or "").lower()
    if fmt not in _FORMATS:
        raise FetchError(f"pick.format must be one of {list(_FORMATS)}, not {pick.get('format')!r}.")
    opts = {k: pick[k] for k in ("encoding", "delimiter") if pick.get(k)}
    raw_sets = pick.get("sets")
    if raw_sets is None:
        # The one-set shorthand: the selector's keys sit beside `format`, and the set is "records".
        raw_sets = {"records": {k: v for k, v in pick.items() if k not in ("format", "encoding", "delimiter")}}
    if not isinstance(raw_sets, dict) or not raw_sets:
        raise FetchError("pick.sets must be an object naming at least one set.")
    return fmt, {str(n): _selector(s, fmt, str(n)) for n, s in raw_sets.items()}, opts


def _passes(sel: dict, value: Any) -> bool:
    if not any(k in sel for k in _TESTS):
        return True
    if value is None:
        return False
    v = str(value)
    if "equals" in sel and v != str(sel["equals"]):
        return False
    if "prefix" in sel and not v.startswith(str(sel["prefix"])):
        return False
    if "contains" in sel and str(sel["contains"]) not in v:
        return False
    return not ("_rx" in sel and not sel["_rx"].search(v))


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el) -> str:
    return html.unescape("".join(el.itertext()).strip())


def _xml_record(el) -> dict:
    """{attrs, children: {name: first text}, child_attrs: {name: attrs of the first}, text}."""
    rec: dict[str, Any] = {"attrs": {_local(k): v for k, v in el.attrib.items()}, "children": {}}
    child_attrs: dict[str, dict] = {}
    for c in el:
        t = _local(c.tag)
        if t in rec["children"]:
            continue
        rec["children"][t] = _text(c)
        if c.attrib:
            child_attrs[t] = {_local(k): v for k, v in c.attrib.items()}
    if child_attrs:
        rec["child_attrs"] = child_attrs
    own = html.unescape((el.text or "").strip())
    if own:
        rec["text"] = own
    return rec


def _pick_xml(stream, sets: dict) -> dict[str, list]:
    import xml.etree.ElementTree as ET

    by_tag: dict[str, list[tuple[str, dict]]] = {}
    for name, sel in sets.items():
        by_tag.setdefault(str(sel["tag"]), []).append((name, sel))
    out: dict[str, list] = {n: [] for n in sets}
    stack: list = []
    collecting = 0  # how many open elements are ones a set collects (their children must survive)
    for event, el in ET.iterparse(stream, events=("start", "end")):
        if event == "start":
            stack.append(el)
            if _local(el.tag) in by_tag:
                collecting += 1
            continue
        stack.pop()
        tag = _local(el.tag)
        if tag in by_tag:
            collecting -= 1
            rec = None
            for name, sel in by_tag[tag]:
                rec = rec or _xml_record(el)
                value = rec["attrs"].get(str(sel["attr"])) if sel.get("attr") else rec.get("text")
                if _passes(sel, value):
                    out[name].append(rec)
        if collecting == 0 and stack:
            # Memory stays flat: a finished element nobody is collecting leaves the tree at once. The
            # parent holds only its unfinished last child, so this remove is cheap.
            stack[-1].remove(el)
    return out


def _text_stream(stream, opts: dict):
    return io.TextIOWrapper(io.BufferedReader(stream), encoding=str(opts.get("encoding") or "utf-8-sig"), newline="")


def _pick_lines(stream, sets: dict, opts: dict) -> dict[str, list]:
    out: dict[str, list] = {n: [] for n in sets}
    for raw in _text_stream(stream, opts):
        line = raw.rstrip("\r\n")
        for name, sel in sets.items():
            if not _passes(sel, line):
                continue
            rec: dict[str, Any] = {"line": line}
            m = sel["_rx"].search(line) if "_rx" in sel else None
            if m and m.groupdict():
                rec["groups"] = m.groupdict()
            elif m and m.groups():
                rec["groups"] = list(m.groups())
            out[name].append(rec)
    return out


def _sniff_delimiter(first_line: str) -> str:
    return max((",", ";", "\t", "|"), key=first_line.count)


def _pick_csv(stream, sets: dict, opts: dict) -> dict[str, list]:
    text = _text_stream(stream, opts)
    first = text.readline()
    delimiter = str(opts.get("delimiter") or _sniff_delimiter(first))
    reader = csv.DictReader(itertools.chain([first], text), delimiter=delimiter)
    header = reader.fieldnames or []
    for name, sel in sets.items():
        col = sel.get("column")
        if col is not None and col not in header:
            raise FetchError(f"pick set {name!r}: the file has no column {col!r}. Its columns: {header}.")
        if col is None and any(k in sel for k in _TESTS):
            raise FetchError(f"pick set {name!r} tests a value but names no `column`. The columns: {header}.")
    out: dict[str, list] = {n: [] for n in sets}
    for row in reader:
        row = {k if k is not None else "_extra": v for k, v in row.items()}
        for name, sel in sets.items():
            if _passes(sel, row.get(sel["column"]) if sel.get("column") else None):
                out[name].append(row)
    return out


def _get(rec: Any, path: str) -> Any:
    """A value from a picked record. On an xml record: `@a` its attribute, `c` the first text of child
    `c`, `c@a` that child's attribute, `#text` its own text. On anything else (or as a fallback): a
    dotted path, digits indexing a list."""
    if isinstance(rec, dict) and "attrs" in rec and "children" in rec:
        if path.startswith("@"):
            return rec["attrs"].get(path[1:])
        if path == "#text":
            return rec.get("text")
        if "@" in path:
            child, _, attr = path.partition("@")
            return (rec.get("child_attrs") or {}).get(child, {}).get(attr)
        if path in rec["children"]:
            return rec["children"][path]
    cur = rec
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _pick_json(open_stream, sets: dict) -> dict[str, list]:
    import ijson

    out: dict[str, list] = {n: [] for n in sets}
    for name, sel in sets.items():
        path = str(sel.get("path") or "").strip(".")
        prefix = f"{path}.item" if path else "item"
        field = sel.get("field")
        if field is None and any(k in sel for k in _TESTS):
            raise FetchError(f"pick set {name!r} tests a value but names no `field` inside the item.")
        with open_stream() as stream:
            for item in ijson.items(stream, prefix, use_float=True):
                if _passes(sel, _get(item, str(field)) if field else None):
                    out[name].append(item)
    return out


# --------------------------------------------------------------------------- hand over


def _shape(records: list, fields: Any) -> list:
    if not fields:
        return records
    if not isinstance(fields, dict):
        raise FetchError('`fields` must map output names to paths, e.g. {"id": "@id", "name": "display-name"}.')
    out = []
    for r in records:
        row = {}
        for k, p in fields.items():
            v = _get(r, str(p))
            if v is not None:
                row[k] = v
        out.append(row)
    return out


def _build_input(map_: Any, picked: dict[str, list]) -> dict:
    """The app tool's input. A map value {"from": set, "fields": {...}} is that set's records, shaped;
    "$set" is the set as picked; anything else is a fixed value. No map: {set: records} for each set."""
    if not map_:
        return dict(picked)
    if not isinstance(map_, dict):
        raise FetchError("pipe_to.map must be an object.")
    out: dict[str, Any] = {}
    for key, spec in map_.items():
        if isinstance(spec, dict) and "from" in spec:
            src = str(spec["from"])
            if src not in picked:
                raise FetchError(f"pipe_to.map.{key} takes from {src!r}, which is not a picked set ({list(picked)}).")
            out[key] = _shape(picked[src], spec.get("fields"))
        elif isinstance(spec, str) and spec.startswith("$") and spec[1:] in picked:
            out[key] = picked[spec[1:]]
        else:
            out[key] = spec
    return out


def _pipe_app_tool(agent_name: str, pipe: dict, picked: dict[str, list]) -> tuple[str, Any]:
    missing = [k for k in ("owner", "app", "tool") if not pipe.get(k)]
    if missing:
        raise FetchError(f"pipe_to names no {', '.join(missing)}: an app tool is {{owner, app, tool, map}}.")
    ref = f"{pipe['owner']}/{pipe['app']}:{pipe['tool']}"
    payload = _build_input(pipe.get("map"), picked)
    from crewaimeat.app_tools import call_app_tool_ref

    answer = call_app_tool_ref(agent_name, ref, payload)
    try:
        return ref, json.loads(answer)
    except ValueError:
        return ref, answer


def _pipe_workspace(agent_name: str, pipe: dict, picked: dict[str, list]) -> tuple[str, Any]:
    from crewaimeat.workspace_tools import WorkspaceWriteError, append_rows, resolve_workspace

    if not pipe.get("space"):
        raise FetchError("pipe_to names a workspace but no `space` (the row space to append to).")
    src = str(pipe.get("from") or (next(iter(picked)) if len(picked) == 1 else ""))
    if src not in picked:
        raise FetchError(f"pipe_to.from must name one picked set ({list(picked)}).")
    hit, why = resolve_workspace(agent_name, str(pipe["workspace"]), str(pipe.get("organism") or ""))
    if hit is None:
        raise FetchError(f"NOT WRITTEN. {why}")
    raw = picked[src]
    bodies = _shape(raw, pipe.get("fields"))
    rows = []
    for rec, body in zip(raw, bodies):
        row: dict[str, Any] = {"body": body}
        if pipe.get("row_id"):
            rid = _get(rec, str(pipe["row_id"]))
            if rid is not None:
                row["row_id"] = str(rid)
        rows.append(row)
    written, ids = 0, 0
    for i in range(0, len(rows), _ROWS_PER_APPEND):
        try:
            done = append_rows(
                agent_name, hit["organism_id"], hit["ws"], str(pipe["space"]), rows[i : i + _ROWS_PER_APPEND]
            )
        except WorkspaceWriteError as exc:
            raise FetchError(f"NOT WRITTEN past row {i}: {exc} ({written} row(s) were written before it).") from exc
        written += int(done.get("written") or 0)
        ids += len(done.get("row_ids") or [])
    where = f"workspace {hit['name']} / {pipe['space']}"
    return where, {"written": written, "row_ids": ids}


# --------------------------------------------------------------------------- refs


def _ref_dir() -> Path:
    from crewaimeat._home import aimeat_home

    d = aimeat_home() / "file_fetch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_ref(source: str, picked: dict[str, list]) -> str:
    d = _ref_dir()
    cutoff = time.time() - _REF_KEEP_S
    for old in d.glob("*.json"):
        with contextlib.suppress(OSError):
            if old.stat().st_mtime < cutoff:
                old.unlink()
    rid = uuid.uuid4().hex[:12]
    (d / f"{rid}.json").write_text(json.dumps({"source": source, "picked": picked}, ensure_ascii=False), "utf-8")
    return f"ref:{rid}"


def _load_ref(ref: str) -> dict:
    rid = ref.split(":", 1)[1].strip()
    if not re.fullmatch(r"[0-9a-f]{12}", rid):
        raise FetchError(f"{ref!r} is not a file_fetch reference (ref:<12 hex>).")
    p = _ref_dir() / f"{rid}.json"
    if not p.exists():
        raise FetchError(f"{ref} is gone (references are kept {_REF_KEEP_S // 86400} days); fetch the URL again.")
    return json.loads(p.read_text("utf-8"))


# --------------------------------------------------------------------------- the call


def fetch(
    agent_name: str,
    url: str,
    pick: Any = None,
    pipe_to: Any = None,
    *,
    max_mb: float = DEFAULT_MAX_MB,
    max_inflated_mb: float = DEFAULT_MAX_INFLATED_MB,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    member: str = "",
) -> dict:
    """Download, unpack, pick, hand over. Returns the summary the model sees; raises FetchError."""
    started = time.monotonic()
    deadline = started + float(timeout_s)
    url = (url or "").strip()
    info: dict[str, Any] = {}
    if url.startswith("ref:"):
        saved = _load_ref(url)
        picked, info["source"] = saved["picked"], f"{url} ({saved.get('source')})"
    else:
        fmt, sets, opts = _parse_pick(pick)
        tmp = tempfile.NamedTemporaryFile(prefix="file_fetch-", suffix=".bin", delete=False)  # noqa: SIM115
        path = Path(tmp.name)
        try:
            with tmp:
                got = _download(url, tmp, int(float(max_mb) * _MB), deadline, float(timeout_s))
            info.update(source=got["url"], raw_bytes=got["bytes"])

            def open_stream():
                stack = contextlib.ExitStack()
                inner, how = _open_payload(path, member, stack)
                info["unpacked"] = how
                bounded = _Bounded(
                    inner, int(float(max_inflated_mb) * _MB), deadline, float(timeout_s), float(max_inflated_mb)
                )
                stack.callback(lambda: info.__setitem__("unpacked_bytes", bounded.count))
                stack.enter_context(contextlib.closing(bounded))
                return _Closing(bounded, stack)

            try:
                if fmt == "json":
                    picked = _pick_json(open_stream, sets)
                else:
                    with open_stream() as stream:
                        if fmt == "xml":
                            picked = _pick_xml(stream, sets)
                        elif fmt == "lines":
                            picked = _pick_lines(stream, sets, opts)
                        else:
                            picked = _pick_csv(stream, sets, opts)
            except UnicodeDecodeError as exc:
                raise FetchError(
                    f"The file is not valid {opts.get('encoding') or 'utf-8'} ({exc.reason} at byte {exc.start}); "
                    f'pass pick.encoding (e.g. "cp1252" or "latin-1").'
                ) from exc
            except CeilingError:
                raise
            except Exception as exc:  # noqa: BLE001 -- a malformed file is named, not a stack trace
                if isinstance(exc, FetchError):
                    raise
                raise FetchError(f"The file could not be read as {fmt}: {type(exc).__name__}: {exc}") from exc
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
    counts = {n: len(r) for n, r in picked.items()}
    out: dict[str, Any] = {**info, "picked": counts}
    out["ref"] = url if url.startswith("ref:") else _save_ref(str(info.get("source")), picked)
    went = "summary"
    if pipe_to:
        if not isinstance(pipe_to, dict):
            raise FetchError("pipe_to must be an object: {owner, app, tool, map} or {workspace, space, from, fields}.")
        where, answer = (_pipe_workspace if pipe_to.get("workspace") else _pipe_app_tool)(agent_name, pipe_to, picked)
        out.update(piped_to=where, answer=answer)
        went = f"piped to {where}"
    else:
        out["first"] = {n: r[:_PREVIEW] for n, r in picked.items()}
    out["seconds"] = round(time.monotonic() - started, 1)
    print(
        f"[file_fetch] {agent_name}: {out.get('source')} raw={out.get('raw_bytes', '-')}B "
        f"unpacked={out.get('unpacked', '-')}/{out.get('unpacked_bytes', '-')}B picked={counts} {went} "
        f"in {out['seconds']}s; the model receives counts{' and the answer' if pipe_to else ' and a preview'} only",
        file=sys.stderr,
    )
    return out


class _Closing:
    """A context manager over the bounded stream that closes everything opened with it."""

    def __init__(self, stream, stack: contextlib.ExitStack):
        self._stream, self._stack = stream, stack

    def __enter__(self):
        return self._stream

    def __exit__(self, *exc):
        self._stack.close()
        return False


def _json_arg(raw: str, what: str) -> Any:
    if not (raw or "").strip():
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise FetchError(f"{what} is not valid JSON: {exc}") from exc


def make_file_fetch_tools(agent_name: str) -> list:
    """The crew_def `file_fetch` tool, bound to `agent_name` (whose identity any pipe_to call runs as)."""
    from crewai.tools import tool

    @tool("file_fetch")
    def file_fetch(
        url: str,
        pick_json: str = "",
        pipe_to_json: str = "",
        max_mb: float = DEFAULT_MAX_MB,
        max_inflated_mb: float = DEFAULT_MAX_INFLATED_MB,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        member: str = "",
    ) -> str:
        """Download a big file, unpack it, pick the records a job needs, and hand them on WITHOUT you
        ever holding the file or the records. You decide what to fetch and where it goes; the tool does
        the bytes. You get back counts, and either the receiving tool's answer or a short preview.

        url: an https URL (gzip and zip are unpacked by their content), or a `ref:...` from an earlier
          file_fetch answer to re-use what it picked without downloading again.
        pick_json: {"format": "xml"|"lines"|"json"|"csv", "sets": {"<name>": <selector>, ...}}.
          xml: {"tag": "programme", "attr": "channel", "prefix": "FI:.", "contains": "Rakuten"} -- each
            record is {attrs, children: {child: first text}, child_attrs: {child: attrs}}.
          lines: {"regex": "..."} -- each record is {line, groups}.
          json: {"path": "data.items", "field": "type", "equals": "x"} -- the items of the array at path.
          csv: {"column": "kunta", "equals": "Espoo"}; add "delimiter"/"encoding" beside "format" if needed.
          Tests (all optional, all must hold): equals, prefix, contains, regex. No test = every one.
        pipe_to_json (optional): where the records go.
          App tool: {"owner": "...", "app": "x.html", "tool": "name", "map": {"<input key>": {"from":
            "<set>", "fields": {"<out>": "<path>"}}, "<other key>": <fixed value>}}. Paths on xml
            records: "@attr", "child", "child@attr", "#text"; on others a dotted path.
          Workspace rows: {"workspace": "<name>", "space": "<row space>", "from": "<set>", "fields":
            {...}, "row_id": "<path that makes re-runs replace instead of duplicate>"}.
          Without pipe_to you get a preview and a ref to pass on.
        max_mb (default 64), max_inflated_mb (default 1024), timeout_s (default 300): ceilings; the
          answer names the one that stopped it. member: the zip member to read (default the first)."""
        try:
            out = fetch(
                agent_name,
                url,
                _json_arg(pick_json, "pick_json"),
                _json_arg(pipe_to_json, "pipe_to_json"),
                max_mb=max_mb,
                max_inflated_mb=max_inflated_mb,
                timeout_s=timeout_s,
                member=member,
            )
        except FetchError as exc:
            print(f"[file_fetch] {agent_name}: {url} stopped: {exc}", file=sys.stderr)
            return f"file_fetch stopped: {exc}"
        return json.dumps(out, ensure_ascii=False)

    return [file_fetch]
