"""lingua_image_gen.py - one-shot image generator for the LINGUA content pack (TARGET-024).

Generates flat, style-consistent vocabulary illustrations via OpenRouter image models, saves a
LOCAL BACKUP to .lingua-images/ (gitignored) and uploads each image PUBLIC to the AIMEAT owner's
storage, printing the /v1/pub/... URL that lingua.catalog can reference per item.

Images are generated ONCE per content pack, never at play time - a Tier 1 batch of ~100 nouns is
a one-time cost of a few dollars at most.

Usage (from the crewfive repo root, where .env lives):
    python scripts/lingua_image_gen.py sun tree fish
    python scripts/lingua_image_gen.py --model google/gemini-2.5-flash-image water house
    python scripts/lingua_image_gen.py --file concepts.txt          # one concept per line
    python scripts/lingua_image_gen.py --dry-run sun                # generate + local save, no upload

Reads from .env: OPENROUTER_API_KEY (generation), AIMEAT_APP_LOGIN_USER / AIMEAT_APP_LOGIN_PASSWORD
(owner login for the public upload - files land under the owner GHII, same home as lingua.catalog).

Concept ids are LANGUAGE-NEUTRAL (english slugs like 'sun'); the words come from the content pack.
Prompt template keeps every image in one visual style so the art style never leaks answer hints.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(REPO, ".env")
BACKUP_DIR = os.path.join(REPO, ".lingua-images")
AIMEAT_BASE = "https://aimeat.io"
OR_BASE = "https://openrouter.ai/api/v1"

# Tried in order until one answers; --model overrides. FLUX.2 Pro first (cheapest per small
# image if available on this key), then Gemini flash image, then Seedream (proven in crewfive).
MODEL_CANDIDATES = [
    "black-forest-labs/flux.2-pro",
    "black-forest-labs/flux-2-pro",
    "google/gemini-2.5-flash-image",
    "bytedance-seed/seedream-4.5",
]

# One shared style for the whole pack: neutral flat icon, nothing textual, nothing decorative
# that could act as a cue beyond the concept itself.
PROMPT = (
    "Minimal flat vector-style illustration of {concept}. A single centered subject or simple "
    "scene on a plain very light neutral background. Clean simple shapes, soft colors, no text, "
    "no letters, no numbers, no border, no watermark."
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


def http_json(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: int = 180):
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


def generate(or_key: str, model: str, concept: str, size: str | None = None) -> tuple[bytes, str, float | None] | None:
    """One image via OpenRouter. With `size` (e.g. '512x512', VERIFIED working on flux.2-pro) the
    UNIFIED IMAGE API is used: POST /images/generations {model, prompt, size} -> data[0].b64_json.
    Note: FLUX.2 Pro bills a flat ~$0.03 minimum whatever the size - a 512x512 request saves
    bandwidth (~77 kB vs ~350 kB), not money. Without `size`, falls back to the chat/completions
    modalities:['image'] pattern proven by crewfive's seedream_gen.
    Returns (bytes, mime, billed_cost_usd) or None on failure."""
    hdrs = {"Authorization": f"Bearer {or_key}", "HTTP-Referer": "https://aimeat.io", "X-Title": "LINGUA image gen"}
    prompt = PROMPT.format(concept=concept)
    if size:
        # BFL moderation throws flaky false positives ("Protected Content") - the same prompt
        # passes on retry, so try up to 3 times before falling back to another model
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
            print(f"    [{model}] images API HTTP {st} (attempt {attempt + 1}) {msg[:100]}", file=sys.stderr)
            if "Moderat" not in msg:
                break
        if st != 200 or not isinstance(resp, dict):
            return None
        data = (resp.get("data") or [{}])[0]
        b64 = data.get("b64_json")
        if not b64 and str(data.get("url", "")).startswith("data:"):
            b64 = data["url"].split("base64,", 1)[1]
        if not b64:
            print(f"    [{model}] no image in images API response", file=sys.stderr)
            return None
        raw = base64.b64decode(b64)
        mime = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        cost = (resp.get("usage") or {}).get("cost")
        return raw, mime, (float(cost) if isinstance(cost, (int, float)) else None)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image"],
        "usage": {"include": True},  # ask OpenRouter to report the real billed cost
    }
    st, resp = http_json("POST", OR_BASE + "/chat/completions", body, hdrs)
    if st != 200 or not isinstance(resp, dict):
        err = (resp or {}).get("error", {}) if isinstance(resp, dict) else {}
        print(f"    [{model}] HTTP {st} {err.get('message', '')[:120]}", file=sys.stderr)
        return None
    try:
        images = resp["choices"][0]["message"].get("images") or []
        data_uri = images[0]["image_url"]["url"]
    except (KeyError, IndexError):
        print(f"    [{model}] no image in response", file=sys.stderr)
        return None
    m = re.match(r"data:(image/[a-z+]+);base64,(.*)$", data_uri, re.S)
    if not m:
        print(f"    [{model}] unexpected image_url format", file=sys.stderr)
        return None
    mime = m.group(1)
    raw = base64.b64decode(m.group(2))
    cost = None
    usage = resp.get("usage") or {}
    if isinstance(usage.get("cost"), (int, float)):
        cost = float(usage["cost"])
    return raw, mime, cost


def owner_token(env: dict) -> tuple[str, str] | None:
    """AIMEAT owner login -> (token, owner GHII). Public files land under the owner, same as
    lingua.catalog (avoid the agent-GAII split GAUGE hit)."""
    # real environment overrides .env (lets a caller inject fresh credentials without editing files)
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
    """Presigned upload (binary stays binary): POST /v1/storage -> PUT raw bytes."""
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
    ap = argparse.ArgumentParser(description="Generate LINGUA vocabulary images (local backup + public AIMEAT storage)")
    ap.add_argument("concepts", nargs="*", help="language-neutral concept slugs, e.g. sun tree fish")
    ap.add_argument("--file", help="file with one concept per line")
    ap.add_argument("--model", help="force one OpenRouter model id")
    ap.add_argument(
        "--size",
        default="1024x1024",
        help="image size via the unified image API (default 1024x1024 = largest at the $0.03 floor; 2048x2048 bills $0.075); pass 'none' for chat-API default",
    )
    ap.add_argument("--dry-run", action="store_true", help="generate + save locally, skip the AIMEAT upload")
    args = ap.parse_args()

    # a concept line may be "slug=prompt phrase" so ambiguous slugs get a precise prompt
    # (e.g. "wolf=a howling gray wolf silhouette") while the file/key name stays the slug
    prompts = {}

    def add(entry):
        if "=" in entry:
            slug, phrase = entry.split("=", 1)
            prompts[slug.strip()] = phrase.strip()
            concepts.append(slug.strip())
        else:
            concepts.append(entry.strip())

    concepts = []
    for c in args.concepts:
        add(c)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    add(ln)
    if not concepts:
        ap.error("give concepts as arguments or via --file")

    env = read_env()
    or_key = env.get("OPENROUTER_API_KEY")
    if not or_key:
        print("OPENROUTER_API_KEY missing from .env", file=sys.stderr)
        return 1

    os.makedirs(BACKUP_DIR, exist_ok=True)
    models = [args.model] if args.model else list(MODEL_CANDIDATES)
    active_model = None
    auth = None
    if not args.dry_run:
        auth = owner_token(env)
        if not auth:
            print("AIMEAT owner login unavailable - rerun with --dry-run or fix .env", file=sys.stderr)
            return 1

    total_cost = 0.0
    results = []
    for concept in concepts:
        slug = re.sub(r"[^a-z0-9-]", "-", concept.lower())
        print(f"[{slug}]")
        out = None
        size = None if (args.size or "").lower() in ("", "none") else args.size
        phrase = prompts.get(concept, concept)
        for m in [active_model] if active_model else models:
            out = generate(or_key, m, phrase, size)
            if out:
                active_model = m
                break
        if not out:
            print("    FAILED (all models)")
            results.append((slug, None, None))
            continue
        raw, mime, cost = out
        if cost:
            total_cost += cost
        ext = _IMAGE_MIMES.get(mime, "png")
        local_path = os.path.join(BACKUP_DIR, f"{slug}.{ext}")
        with open(local_path, "wb") as f:
            f.write(raw)
        url = None
        if not args.dry_run:
            key = f"lingua.img.{slug}"
            if upload_public(auth[0], key, raw, mime):
                url = f"{AIMEAT_BASE}/v1/pub/{urllib.parse.quote(auth[1], safe='')}/{key}"
        print(f"    model={active_model} bytes={len(raw)} cost={f'${cost:.4f}' if cost is not None else '?'}")
        print(f"    local={os.path.relpath(local_path, REPO)}")
        if url:
            print(f"    public={url}")
        results.append((slug, local_path, url))

    ok = sum(1 for _, p, _ in results if p)
    print(f"\ndone: {ok}/{len(concepts)} images, model={active_model}, total billed cost ~${total_cost:.4f}")
    print("catalog snippet (add per item to lingua.catalog):")
    for slug, _p, url in results:
        if url:
            print(f'  "{slug}": {{ "img": "{url}" }}')
    return 0 if ok == len(concepts) else 2


if __name__ == "__main__":
    sys.exit(main())
