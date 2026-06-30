"""map_snapshot — geocode a location, render a static OpenStreetMap image, and store it.

Fully open + no API key: **Nominatim** for geocoding, **OpenStreetMap tiles** stitched into one image
with Pillow (already a dependency — no extra package, so it works on the installed appliance offline of
any pip index). The rendered PNG is uploaded to the agent's node storage (public, the PRESIGNED way so
binary stays binary), and a memory entry linking the image (address, lat/long, precision, timestamp) is
written + published — so an **aimeat.io app can read the memory and show the maps**. A local copy is kept
too. This is deterministic code (the model only chooses the location + precision), mirroring the
content-pipeline pattern, so the result is reliable.

OSM usage policy: we send an identifying User-Agent and this is light, occasional use (snapshots), which
is within the tile + Nominatim fair-use terms.
"""

from __future__ import annotations

import io
import math
import sys
import time

import requests

_UA = "aimeat-agency-map/1.0 (+https://aimeat.io)"  # OSM tile + Nominatim policy: identify yourself
_TILE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# Named precision -> OSM zoom. The label is the approximate width of area the image shows.
_ZOOM = {"20km": 11, "5km": 13, "500m": 16}
_DEFAULT_PRECISION = "5km"


def zoom_for(precision: str) -> int:
    return _ZOOM.get((precision or _DEFAULT_PRECISION).strip().lower(), _ZOOM[_DEFAULT_PRECISION])


def geocode(query: str) -> tuple[float, float, str] | None:
    """An address/place name -> (lat, lon, display_name). Accepts a direct 'lat,lon' too. None on miss."""
    q = (query or "").strip()
    parts = q.replace(" ", "").split(",")
    if len(parts) == 2:  # already coordinates?
        try:
            lat, lon = float(parts[0]), float(parts[1])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon, f"{lat:.5f}, {lon:.5f}"
        except ValueError:
            pass
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 1},
            headers={"User-Agent": _UA},
            timeout=20,
        )
        arr = r.json() if r.status_code == 200 else []
        if arr:
            it = arr[0]
            return float(it["lat"]), float(it["lon"]), it.get("display_name") or q
    except Exception as exc:  # noqa: BLE001
        print(f"[map] geocode failed for {q!r}: {exc!r}", file=sys.stderr)
    return None


def _deg2px(lat: float, lon: float, zoom: float) -> tuple[float, float]:
    """(lat,lon) -> global pixel coords at this zoom (256px tiles, Web Mercator)."""
    lat_r = math.radians(lat)
    n = 2**zoom
    x = (lon + 180.0) / 360.0 * n * 256
    y = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n * 256
    return x, y


def render_png(lat: float, lon: float, zoom: int, width: int = 640, height: int = 480) -> bytes | None:
    """Stitch the OSM tiles covering a width x height window centered on (lat,lon) into one PNG, with a
    small marker on the center point. None if rendering fails."""
    from PIL import Image, ImageDraw

    try:
        cx, cy = _deg2px(lat, lon, zoom)
        x0, y0 = cx - width / 2, cy - height / 2  # top-left of the window, in global px
        canvas = Image.new("RGB", (width, height), (236, 236, 236))
        n = 2**zoom
        tx0, tx1 = int(x0 // 256), int((x0 + width) // 256)
        ty0, ty1 = int(y0 // 256), int((y0 + height) // 256)
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                if not (0 <= ty < n):
                    continue  # off the top/bottom of the world
                try:
                    tr = requests.get(_TILE.format(z=zoom, x=tx % n, y=ty), headers={"User-Agent": _UA}, timeout=20)
                    if tr.status_code != 200:
                        continue
                    tile = Image.open(io.BytesIO(tr.content)).convert("RGB")
                except Exception:  # noqa: BLE001 — a missing tile just leaves a gap
                    continue
                canvas.paste(tile, (int(tx * 256 - x0), int(ty * 256 - y0)))
        d = ImageDraw.Draw(canvas)
        mx, my = width // 2, height // 2
        d.ellipse([mx - 7, my - 7, mx + 7, my + 7], outline=(220, 30, 90), width=3)
        d.line([mx, my - 12, mx, my + 12], fill=(220, 30, 90), width=1)
        d.line([mx - 12, my, mx + 12, my], fill=(220, 30, 90), width=1)
        out = io.BytesIO()
        canvas.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        print(f"[map] render failed: {exc!r}", file=sys.stderr)
        return None


def _own_gaii(agent: str) -> str | None:
    """The agent's GAII, for building a public /v1/pub/<gaii>/<key> read URL."""
    from crewaimeat.aimeat_crew import _aimeat_call

    data = _aimeat_call(agent, "aimeat_agents_list", {}) or {}
    for a in data.get("agents") or []:
        if a.get("name") == agent and a.get("gaii"):
            return a["gaii"]
    return None


def upload_public(agent: str, key: str, image: bytes, mime: str = "image/png") -> str | None:
    """Upload the PNG to the agent's node storage, public, the PRESIGNED way (binary stays binary):
    POST /v1/storage {mode:'presigned'} -> PUT the raw bytes to the returned upload_url. Returns the
    public read URL (/v1/pub/<gaii>/<key>) or None. Mirrors image_contract._upload_public, parameterized
    by agent so any appliance agent can use it."""
    from crewaimeat.aimeat_crew import _serve_api
    from crewaimeat.generator_tool import _discover_owner, _token

    presign = {"key": key, "mime_type": mime, "visibility": "public", "mode": "presigned"}
    try:
        api = _serve_api()
        if api is not None:
            base, session = api
            r = session.post(f"{base}/v1/storage", json=presign, headers={"X-Aimeat-Agent": agent}, timeout=60)
        else:
            tok, url = _token(agent, _discover_owner(agent))
            if not tok or not url:
                return None
            r = requests.post(
                f"{url.rstrip('/')}/v1/storage", json=presign, headers={"Authorization": f"Bearer {tok}"}, timeout=60
            )
        upload_url = ((r.json() or {}).get("data") or {}).get("upload_url") if r.status_code == 200 else None
        if not upload_url:
            print(f"[map] presign {key} failed: HTTP {r.status_code} {r.text[:160]}", file=sys.stderr)
            return None
        put = requests.put(upload_url, data=image, headers={"Content-Type": mime}, timeout=180)
        if put.status_code not in (200, 201):
            print(f"[map] PUT {key} failed: HTTP {put.status_code}", file=sys.stderr)
            return None
        gaii = _own_gaii(agent)
        _tok, node = _token(agent, _discover_owner(agent))
        if gaii and node:
            return f"{node.rstrip('/')}/v1/pub/{gaii}/{key}"
        return key  # uploaded, but the public URL couldn't be composed
    except Exception as exc:  # noqa: BLE001
        print(f"[map] upload {key} failed: {exc!r}", file=sys.stderr)
        return None


def _save_local(key: str, image: bytes) -> str:
    """Keep a local copy under <AIMEAT_HOME>/media. Returns the file path."""
    from crewaimeat._home import aimeat_home

    media = aimeat_home() / "media"
    media.mkdir(parents=True, exist_ok=True)
    path = media / f"{key}.png"
    path.write_bytes(image)
    return str(path)


def make_map_tools(agent_name: str) -> list:
    """One LLM tool: map_snapshot(location, precision). It does the whole deterministic job — geocode,
    render, upload (public), save a local copy, and write + publish a memory entry linking the image —
    so the model only has to pick the location + precision."""
    from crewai.tools import tool

    from crewaimeat.local_memory import publish, remember

    @tool("map_snapshot")
    def map_snapshot(location: str, precision: str = _DEFAULT_PRECISION) -> str:
        """Take a static map image of a LOCATION (an address / place name, or 'lat,lon') at a PRECISION
        — one of '20km', '5km', '500m' (the approximate area shown). Renders an OpenStreetMap image,
        uploads it to public node storage, keeps a local copy, and writes a memory entry linking the
        image with the address, coordinates, precision and timestamp. Returns a readable summary."""
        geo = geocode(location)
        if not geo:
            return f"Could not find a location for '{location}'. Try a more specific address or 'lat,lon'."
        lat, lon, label = geo
        prec = (precision or _DEFAULT_PRECISION).strip().lower()
        if prec not in _ZOOM:
            prec = _DEFAULT_PRECISION
        zoom = zoom_for(prec)
        png = render_png(lat, lon, zoom)
        if not png:
            return f"Found {label} ({lat:.5f},{lon:.5f}) but the map image could not be rendered."
        ts = time.strftime("%Y-%m-%dT%H-%M-%S")
        key = f"maps.{agent_name}.{ts}"
        local_path = _save_local(key, png)
        url = upload_public(agent_name, f"{key}.png", png)
        body = (
            f"Map snapshot of {label}\n"
            f"coordinates: {lat:.5f}, {lon:.5f}\n"
            f"precision: {prec} (OSM zoom {zoom})\n"
            f"captured: {ts}\n"
            f"image: {url or local_path}\n"
            f"local_file: {local_path}"
        )
        rid = remember(agent_name, body, topic="map", source=(url or local_path), tags=["map", prec])
        pub = publish(agent_name, rid, key=key, visibility="public")
        where = url or f"saved locally at {local_path}"
        published = " and published so an app can read it" if (isinstance(pub, dict) and pub.get("ok")) else ""
        return (
            f"Map snapshot taken of {label} ({lat:.5f}, {lon:.5f}) at {prec} precision. "
            f"Image: {where}. A memory entry linking it was saved{published}."
        )

    return [map_snapshot]
