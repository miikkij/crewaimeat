"""file_fetch: download, unpack, pick while streaming, and hand over without the model carrying the data.

Served from a loopback server this test starts (the host allowed through FILE_FETCH_ALLOW_HOSTS, the way
an owner would allow one), so every byte path is the real one: requests streaming, gzip/zip by content,
expat/ijson/csv pickers, the ceilings. The node is stood in for at the two seams a pipe calls through.
"""

from __future__ import annotations

import gzip
import http.server
import io
import json
import threading
import tracemalloc
import zipfile

import pytest

import crewaimeat.file_fetch as ff

XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="test">
  <channel id="FI:.Rakuten.VIKI.be">
    <display-name lang="en">FI: Rakuten VIKI</display-name>
    <icon src="https://img/viki.jpeg" />
    <url>http://www.rakuten.tv</url>
  </channel>
  <channel id="DE:.Rakuten.Action.de"><display-name>DE: Action</display-name></channel>
  <channel id="FI:.Yle.TV1"><display-name>FI: Yle</display-name></channel>
  <programme channel="FI:.Rakuten.VIKI.be" start="20261010002337 -0300" stop="20261010014722 -0300">
    <title lang="fi">Graceful family</title><title lang="en">ignored second title</title>
    <desc lang="fi">Chairman Mo &amp;amp; Seok Hui</desc>
    <category lang="en">Drama</category>
  </programme>
  <programme start="20261010014728 -0300" stop="20261010031021 -0300" channel="FI:.Rakuten.VIKI.be">
    <title>Second</title>
  </programme>
  <programme channel="DE:.Rakuten.Action.de" start="20261010000000 +0200" stop="20261010010000 +0200">
    <title>Nicht</title>
  </programme>
</tv>
"""

FI_SETS = {
    "format": "xml",
    "sets": {
        "channels": {"tag": "channel", "attr": "id", "prefix": "FI:.", "contains": "Rakuten"},
        "programmes": {"tag": "programme", "attr": "channel", "prefix": "FI:.", "contains": "Rakuten"},
    },
}

INGEST_MAP = {
    "tag": "RAKUTEN",
    "source": "test FI section",
    "channels": {"from": "channels", "fields": {"id": "@id", "name": "display-name", "icon": "icon@src"}},
    "programmes": {
        "from": "programmes",
        "fields": {
            "channel": "@channel",
            "start": "@start",
            "stop": "@stop",
            "title": "title",
            "desc": "desc",
            "category": "category",
        },
    },
}


def _zip(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("readme.txt", b"not this one")
        z.writestr(name, data)
    return buf.getvalue()


CSV = b"kunta;asukkaat;koodi\nEspoo;305000;049\nVantaa;250000;092\nEspoonlahti;1;999\n"
JSONDOC = json.dumps({"data": {"items": [{"id": 1, "type": "a"}, {"id": 2, "type": "b"}, {"id": 3, "type": "a"}]}})
LINES = "ok one\nERROR 42 disk\nok two\nERROR 7 net\n"


@pytest.fixture
def server(monkeypatch, tmp_path):
    """A loopback server with fixed routes; yields its base URL. The host is allowed as an owner would."""
    routes: dict[str, tuple[int, dict, bytes]] = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            status, headers, body = routes.get(self.path, (404, {}, b"no"))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("FILE_FETCH_ALLOW_HOSTS", "127.0.0.1")
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "home"))

    def add(path, body: bytes, status=200, length=True, headers=None):
        h = dict(headers or {})
        if length:
            h["Content-Length"] = str(len(body))
        routes[path] = (status, h, body)
        return base + path

    yield add
    srv.shutdown()


pytestmark = pytest.mark.loopback


def test_xmltv_gzip_picks_the_finnish_rakuten_part(server):
    url = server("/guide.xml.gz", gzip.compress(XMLTV.encode()))
    out = ff.fetch("feeder", url, FI_SETS)
    assert out["unpacked"] == "gzip"
    assert out["picked"] == {"channels": 1, "programmes": 2}
    ch = out["first"]["channels"][0]
    assert ch["attrs"]["id"] == "FI:.Rakuten.VIKI.be"
    assert ch["children"]["display-name"] == "FI: Rakuten VIKI"
    assert ch["child_attrs"]["icon"]["src"] == "https://img/viki.jpeg"
    first, second = out["first"]["programmes"]
    assert first["children"]["title"] == "Graceful family"  # the FIRST title, not the second
    assert first["children"]["desc"] == "Chairman Mo & Seok Hui"  # entities decoded, double-escaped too
    # attribute order varies in the real file (`channel` before or after `start`): both are picked
    assert second["attrs"]["start"] == "20261010014728 -0300"
    assert out["ref"].startswith("ref:")


def test_pipe_to_app_tool_maps_records_and_returns_only_the_answer(server, monkeypatch):
    seen = {}

    def fake_call(agent, ref, payload):
        seen.update(agent=agent, ref=ref, payload=payload)
        return json.dumps({"tag": "RAKUTEN", "channels": 1, "programmes": 2, "days": ["2026-10-10"]})

    monkeypatch.setattr("crewaimeat.app_tools.call_app_tool_ref", fake_call)
    url = server("/guide.xml.gz", gzip.compress(XMLTV.encode()))
    pipe = {"owner": "happydude500001", "app": "tv-opas.html", "tool": "ingestGuide", "map": INGEST_MAP}
    out = ff.fetch("feeder", url, FI_SETS, pipe)

    assert seen["ref"] == "happydude500001/tv-opas.html:ingestGuide"
    p = seen["payload"]
    assert p["tag"] == "RAKUTEN" and p["source"] == "test FI section"
    assert p["channels"] == [{"id": "FI:.Rakuten.VIKI.be", "name": "FI: Rakuten VIKI", "icon": "https://img/viki.jpeg"}]
    assert p["programmes"][0] == {
        "channel": "FI:.Rakuten.VIKI.be",
        "start": "20261010002337 -0300",
        "stop": "20261010014722 -0300",
        "title": "Graceful family",
        "desc": "Chairman Mo & Seok Hui",
        "category": "Drama",
    }
    assert p["programmes"][1] == {  # a missing child is left out, not sent as null
        "channel": "FI:.Rakuten.VIKI.be",
        "start": "20261010014728 -0300",
        "stop": "20261010031021 -0300",
        "title": "Second",
    }
    # What the MODEL gets: counts and the tool's answer -- no records.
    assert out["answer"]["programmes"] == 2 and out["piped_to"] == seen["ref"]
    assert "first" not in out
    assert "Graceful" not in json.dumps(out)


def test_the_tool_answer_carries_no_records_and_the_log_says_so(server, monkeypatch, capsys):
    monkeypatch.setattr("crewaimeat.app_tools.call_app_tool_ref", lambda *a: '{"ok": true}')
    url = server("/guide.xml.gz", gzip.compress(XMLTV.encode()))
    (tool,) = ff.make_file_fetch_tools("feeder")
    pipe = {"owner": "o", "app": "a.html", "tool": "t", "map": INGEST_MAP}
    answer = tool.func(url, json.dumps(FI_SETS), json.dumps(pipe))
    assert json.loads(answer)["picked"] == {"channels": 1, "programmes": 2}
    assert "Graceful" not in answer
    log = capsys.readouterr().err
    assert "[file_fetch] feeder:" in log and "piped to o/a.html:t" in log and "counts" in log


def test_a_ref_is_read_again_without_downloading(server, monkeypatch):
    url = server("/guide.xml.gz", gzip.compress(XMLTV.encode()))
    first = ff.fetch("feeder", url, FI_SETS)
    got = {}
    monkeypatch.setattr("crewaimeat.app_tools.call_app_tool_ref", lambda a, r, p: got.update(p) or "{}")
    again = ff.fetch("feeder", first["ref"], None, {"owner": "o", "app": "a", "tool": "t", "map": INGEST_MAP})
    assert again["picked"] == first["picked"] and "raw_bytes" not in again
    assert len(got["programmes"]) == 2


def test_gzip_bomb_stops_at_the_unpacked_ceiling(server):
    # Valid XML that inflates to 64 MB from a few hundred kB: the parser would read it all happily.
    bomb = gzip.compress(b"<tv>" + b"<x/>" * (16 * 1024 * 1024) + b"</tv>", compresslevel=9)
    assert len(bomb) < 1024 * 1024
    url = server("/bomb.xml.gz", bomb)
    with pytest.raises(ff.CeilingError, match=r"unpacked ceiling of 8 MB \(max_inflated_mb\)"):
        ff.fetch("feeder", url, FI_SETS, max_inflated_mb=8)


@pytest.mark.parametrize("declared", [True, False])
def test_an_oversize_file_stops_at_the_download_ceiling(server, declared):
    url = server("/big.bin", b"x" * (3 * 1024 * 1024), length=declared)
    with pytest.raises(ff.CeilingError, match=r"download ceiling of 1\.0 MB \(max_mb\)"):
        ff.fetch("feeder", url, {"format": "lines", "regex": "x"}, max_mb=1)


def test_the_tool_names_the_ceiling_to_the_model(server):
    url = server("/big.bin", b"x" * (3 * 1024 * 1024))
    (tool,) = ff.make_file_fetch_tools("feeder")
    said = tool.func(url, '{"format": "lines", "regex": "x"}', "", 1)
    assert said.startswith("file_fetch stopped: Stopped at the download ceiling of 1.0 MB (max_mb)")


def test_zip_member_csv_rows_where_a_column_matches(server):
    url = server("/data.zip", _zip("kunnat.csv", CSV))
    out = ff.fetch("feeder", url, {"format": "csv", "column": "kunta", "prefix": "Espoo"}, member="kunnat.csv")
    assert out["unpacked"] == "zip:kunnat.csv"
    assert [r["kunta"] for r in out["first"]["records"]] == ["Espoo", "Espoonlahti"]  # ';' was sniffed


def test_csv_names_the_columns_when_the_one_asked_for_is_missing(server):
    url = server("/k.csv", CSV)
    with pytest.raises(ff.FetchError, match=r"no column 'town'.*'kunta'"):
        ff.fetch("feeder", url, {"format": "csv", "column": "town", "equals": "Espoo"})


def test_json_items_under_a_path(server):
    url = server("/d.json", JSONDOC.encode())
    out = ff.fetch("feeder", url, {"format": "json", "path": "data.items", "field": "type", "equals": "a"})
    assert [r["id"] for r in out["first"]["records"]] == [1, 3]


def test_lines_by_regex_with_groups(server):
    url = server("/log.txt", LINES.encode())
    out = ff.fetch("feeder", url, {"format": "lines", "sets": {"errors": {"regex": r"ERROR (?P<code>\d+)"}}})
    assert [r["groups"]["code"] for r in out["first"]["errors"]] == ["42", "7"]


def test_csv_piped_into_a_workspace_row_space_in_batches(server, monkeypatch):
    """The second job with no new code: an open-data CSV into a workspace through workspace_write's path."""
    big = "id;nimi\n" + "".join(f"{i};paikka {i}\n" for i in range(1200))
    url = server("/paikat.csv", big.encode())
    calls = []
    monkeypatch.setattr(
        "crewaimeat.workspace_tools.resolve_workspace",
        lambda agent, name, org="": ({"organism_id": "org", "ws": "ws-1", "name": name}, ""),
    )

    def fake_append(agent, org, ws, space, rows):
        calls.append((ws, space, rows))
        return {"written": len(rows), "row_ids": [r.get("row_id") for r in rows]}

    monkeypatch.setattr("crewaimeat.workspace_tools.append_rows", fake_append)
    pipe = {"workspace": "Avoin data", "space": "paikka", "fields": {"name": "nimi"}, "row_id": "id"}
    out = ff.fetch("feeder", url, {"format": "csv"}, pipe)
    assert [len(c[2]) for c in calls] == [500, 500, 200]  # the node's documented 500 per append
    assert calls[0][2][0] == {"body": {"name": "paikka 0"}, "row_id": "0"}
    assert out["answer"] == {"written": 1200, "row_ids": 1200}
    assert out["piped_to"] == "workspace Avoin data / paikka"


def test_memory_stays_flat_on_a_big_file(server):
    """~24 MB of XML of which nothing is picked: the parser must not grow with the file."""
    filler = "".join(f'<programme channel="DE:.x" start="{i}"><title>t{i}</title></programme>' for i in range(4000))
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
        gz.write(b"<tv>")
        for _ in range(80):
            gz.write(filler.encode())
        gz.write(b'<programme channel="FI:.Rakuten.X" start="1" stop="2"><title>kept</title></programme></tv>')
    url = server("/huge.xml.gz", raw.getvalue())
    tracemalloc.start()
    try:
        out = ff.fetch("feeder", url, FI_SETS)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert out["unpacked_bytes"] > 20 * 1024 * 1024
    assert out["picked"]["programmes"] == 1
    assert peak < 16 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB grows with the file"


def test_http_and_inside_addresses_need_the_owner(monkeypatch):
    monkeypatch.delenv("FILE_FETCH_ALLOW_HOSTS", raising=False)
    with pytest.raises(ff.FetchError, match="plain http"):
        ff.fetch("feeder", "http://example.org/x.gz", FI_SETS)
    monkeypatch.setattr(ff.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 443))])
    with pytest.raises(ff.FetchError, match="169.254.169.254, an address inside"):
        ff.fetch("feeder", "https://metadata.example/x", FI_SETS)


def test_a_redirect_hop_is_checked_too(server, monkeypatch):
    url = server("/go", b"", status=302, headers={"Location": "http://elsewhere.example/x"})
    with pytest.raises(ff.FetchError, match="elsewhere.example/x is plain http"):
        ff.fetch("feeder", url, FI_SETS)


def test_pick_is_required_and_explained():
    with pytest.raises(ff.FetchError, match="pick is required"):
        ff.fetch("feeder", "https://example.org/x", None)
    with pytest.raises(ff.FetchError, match="pick.format must be one of"):
        ff.fetch("feeder", "https://example.org/x", {"format": "pdf"})


def test_call_app_tool_ref_resolves_and_invokes(monkeypatch):
    import crewaimeat.app_tools as at

    entry = {
        "sku": "app-tool:me/tv.html:ingest",
        "app": "me/tv.html",
        "name": "ingest",
        "ownerName": "me",
        "webmcp": {"invoke": "https://node/v1/apps/me/tv.html/webmcp/tools/ingest"},
    }
    monkeypatch.setattr(at, "_catalog", lambda agent, owner="": [entry])
    posted = {}

    def fake_rest(agent, method, path, body=None, **kw):
        posted.update(method=method, path=path, body=body)
        return {"result": {"programmes": 2}}

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_rest", fake_rest)
    assert json.loads(at.call_app_tool_ref("a#me@n", "me/tv.html:ingest", {"x": 1})) == {"programmes": 2}
    assert posted == {"method": "POST", "path": "/v1/apps/me/tv.html/webmcp/tools/ingest", "body": {"x": 1}}
    assert at.call_app_tool_ref("a#me@n", "other:tool", {}).startswith("No single app-tool matches")


def test_file_fetch_is_in_the_runtime_menu():
    from crewaimeat.crew_def import TOOL_PURPOSES, resolve_tool

    assert resolve_tool("file_fetch") is not None
    assert "pipe_to_json" in TOOL_PURPOSES["file_fetch"] and "max_inflated_mb" in TOOL_PURPOSES["file_fetch"]
