"""ec_image_gen.py - illustration generator for the AIMEAT Experience Center curriculum.

Sibling of lingua_image_gen.py: generates style-consistent illustrations via OpenRouter image
models from a prompts file (record-id=prompt, see scripts/ec-image-prompts.txt), saves a LOCAL
BACKUP to .ec-images/ (gitignored) and uploads each image PUBLIC to the AIMEAT owner's storage
as key `ec.img.<record-id>`, printing the /v1/pub/... URL to set as the record's image_url.

Differences from the LINGUA generator:
  - a shared Experience Center STYLE PREFIX (flat tech-brand style, EC palette, wide banner)
  - text is ALLOWED in images (short labels like GHII, GAII, v1) - the record prompt decides
  - tries a wide size first (banner cards), falls back to 1024x1024 if the model rejects it

Usage (from the crewfive repo root, where .env lives):
    python scripts/ec_image_gen.py --file scripts/ec-image-prompts.txt
    python scripts/ec_image_gen.py --file scripts/ec-image-prompts.txt --only sl-l0-identities
    python scripts/ec_image_gen.py --dry-run --file scripts/ec-image-prompts.txt

Reads from .env: OPENROUTER_API_KEY (generation), AIMEAT_APP_LOGIN_USER / AIMEAT_APP_LOGIN_PASSWORD
(owner login for the public upload - files land under the owner GHII).

After a run, apply the printed image_url values to the curriculum records (Experience Center
organism d0d999ef-712b-408b-91e9-e074cfac3bae, workspace ws-mrp3eu8k1r1) via the
experience-center-authoring skill - records keep the same prompt in image_prompt.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(REPO, ".env")
BACKUP_DIR = os.path.join(REPO, ".ec-images")
AIMEAT_BASE = "https://aimeat.io"
OR_BASE = "https://openrouter.ai/api/v1"

MODEL_CANDIDATES = [
    "black-forest-labs/flux.2-pro",
    "black-forest-labs/flux-2-pro",
    "google/gemini-2.5-flash-image",
    "bytedance-seed/seedream-4.5",
]

# One shared style for the whole curriculum. Text labels are allowed when the record prompt
# asks for them - keep them few and short (image models bungle long text).
STYLE = (
    "Modern flat vector illustration in a consistent friendly tech-brand style, soft rounded "
    "geometric shapes, clean wide banner composition on a very light neutral background. "
    "Color palette: blue #5B9DFF for humans, green #2EA043 for robots and agents, amber #F0A020 "
    "for apps, keys and highlights, red #E8564A for accents and arrows, gray #94A3B1 outlines. "
    "Crisp and minimal, no watermark, no border. "
)

_IMAGE_MIMES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
CTX = ssl.create_default_context()


def read_env() -> dict:
    vals = {}
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def http_json(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: int = 240):
    req = urllib.request.Request(
        url, data=(json.dumps(body).encode("utf-8") if body is not None else None), method=method
    )
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, None


def _images_api(or_key: str, model: str, prompt: str, size: str):
    hdrs = {"Authorization": f"Bearer {or_key}", "HTTP-Referer": "https://aimeat.io", "X-Title": "EC image gen"}
    # BFL moderation throws flaky false positives - retry up to 3 times (lingua-proven)
    st, resp = None, None
    for attempt in range(3):
        st, resp = http_json(
            "POST",
            OR_BASE + "/images/generations",
            {"model": model, "prompt": prompt, "size": size, "usage": {"include": True}},
            hdrs,
        )
        if st == 200:
            break
        msg = str((resp or {}).get("error", {}))[:160] if isinstance(resp, dict) else ""
        print(f"    [{model}] images API HTTP {st} size={size} (attempt {attempt + 1}) {msg[:100]}", file=sys.stderr)
        if "Moderat" not in msg:
            break
    if st != 200 or not isinstance(resp, dict):
        return None
    data = (resp.get("data") or [{}])[0]
    b64 = data.get("b64_json")
    if not b64 and str(data.get("url", "")).startswith("data:"):
        b64 = data["url"].split("base64,", 1)[1]
    if not b64:
        return None
    raw = base64.b64decode(b64)
    mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    cost = (resp.get("usage") or {}).get("cost")
    return raw, mime, (float(cost) if isinstance(cost, (int, float)) else None)


def generate(or_key: str, model: str, record_prompt: str, sizes: list[str]):
    """Try the requested sizes in order (wide first, square fallback) via the unified image API."""
    prompt = STYLE + record_prompt
    for size in sizes:
        out = _images_api(or_key, model, prompt, size)
        if out:
            return out
    return None


def owner_token(env: dict):
    user = os.environ.get("AIMEAT_APP_LOGIN_USER") or env.get("AIMEAT_APP_LOGIN_USER")
    pw = os.environ.get("AIMEAT_APP_LOGIN_PASSWORD") or env.get("AIMEAT_APP_LOGIN_PASSWORD")
    if not user or not pw:
        return None
    st, resp = http_json("POST", AIMEAT_BASE + "/v1/ghii/login", {"username": user, "password": pw})
    if st != 200:
        print(f"AIMEAT login failed: HTTP {st}", file=sys.stderr)
        return None
    data = resp.get("data") or {}
    token = data.get("token")
    ghii = (
        (data.get("ghii") or {}).get("gaii")
        or (data.get("ghii") or {}).get("id")
        or f"{user}@aimeat-finland-001-genesis"
    )
    return token, ghii


def upload_public(token: str, key: str, raw: bytes, mime: str) -> bool:
    st, resp = http_json(
        "POST",
        AIMEAT_BASE + "/v1/storage",
        {"key": key, "mime_type": mime, "visibility": "public", "mode": "presigned"},
        {"Authorization": f"Bearer {token}"},
    )
    upload_url = ((resp or {}).get("data") or {}).get("upload_url") if st == 200 else None
    if not upload_url:
        print(f"    presign failed: HTTP {st}", file=sys.stderr)
        return False
    req = urllib.request.Request(upload_url, data=raw, method="PUT")
    req.add_header("Content-Type", mime)
    try:
        with urllib.request.urlopen(req, timeout=120, context=CTX) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        print(f"    PUT failed: HTTP {e.code}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate Experience Center illustrations (local backup + public AIMEAT storage)"
    )
    ap.add_argument(
        "--file",
        default=os.path.join("scripts", "ec-image-prompts.txt"),
        help="prompts file: <record-id>=<prompt> per line",
    )
    ap.add_argument("--only", action="append", help="limit to these record ids (repeatable)")
    ap.add_argument("--model", help="force one OpenRouter model id")
    ap.add_argument("--size", default="1344x576", help="preferred (wide) size; falls back to 1024x1024 automatically")
    ap.add_argument("--dry-run", action="store_true", help="generate + save locally, skip the AIMEAT upload")
    args = ap.parse_args()

    prompts: dict[str, str] = {}
    order: list[str] = []
    with open(args.file, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            rid, phrase = ln.split("=", 1)
            rid = rid.strip()
            prompts[rid] = phrase.strip()
            order.append(rid)
    if args.only:
        order = [r for r in order if r in set(args.only)]
    if not order:
        ap.error("no record ids selected")

    env = read_env()
    or_key = env.get("OPENROUTER_API_KEY")
    if not or_key:
        print("OPENROUTER_API_KEY missing from .env", file=sys.stderr)
        return 1

    os.makedirs(BACKUP_DIR, exist_ok=True)
    models = [args.model] if args.model else list(MODEL_CANDIDATES)
    sizes = [args.size, "1024x1024"] if args.size and args.size != "1024x1024" else ["1024x1024"]
    active_model = None
    auth = None
    if not args.dry_run:
        auth = owner_token(env)
        if not auth:
            print("AIMEAT owner login unavailable - rerun with --dry-run or fix .env", file=sys.stderr)
            return 1

    total_cost = 0.0
    results = []
    for rid in order:
        print(f"[{rid}]")
        out = None
        for m in [active_model] if active_model else models:
            out = generate(or_key, m, prompts[rid], sizes)
            if out:
                active_model = m
                break
        if not out:
            print("    FAILED (all models)")
            results.append((rid, None))
            continue
        raw, mime, cost = out
        if cost:
            total_cost += cost
        ext = _IMAGE_MIMES.get(mime, "png")
        local_path = os.path.join(BACKUP_DIR, f"{rid}.{ext}")
        with open(local_path, "wb") as f:
            f.write(raw)
        url = None
        if not args.dry_run:
            key = f"ec.img.{rid}"
            if upload_public(auth[0], key, raw, mime):
                url = f"{AIMEAT_BASE}/v1/pub/{urllib.parse.quote(auth[1], safe='')}/{key}"
        print(f"    model={active_model} bytes={len(raw)} cost={f'${cost:.4f}' if cost is not None else '?'}")
        if url:
            print(f"    public={url}")
        results.append((rid, url))

    ok = sum(1 for _, u in results if u or args.dry_run)
    print(f"\ndone: {ok}/{len(order)} images, model={active_model}, total billed cost ~${total_cost:.4f}")
    print("record mapping (set as image_url via the experience-center-authoring skill):")
    for rid, url in results:
        if url:
            print(f"  {rid} -> {url}")
    return 0 if ok == len(order) else 2


if __name__ == "__main__":
    sys.exit(main())
