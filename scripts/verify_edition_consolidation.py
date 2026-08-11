"""Verify ONE evening edition after the consolidation. Read-only — it writes nothing.

    uv run python scripts/verify_edition_consolidation.py [YYYY-MM-DD] [evening]

Checks, in the order they can fail:
  1. the edition lists ~25 keys, not 44, and EXACTLY ONE ends in ".raw"
  2. that raw record has >= 12 non-empty categories and is under 800 kB
  3. the status record is ONE record under the OWNER GHII — not six under six agent namespaces —
     with all six fields at "done"
  4. every workflow step's signals are GREEN (no step input-red or output-red)

Reads go DIRECT to the node with news-fetcher's token, so this works off-fleet: the connector tool
surface returns empty lists when the agent is not attached, which would read as "everything is
missing" and is exactly the wrong answer from a verification script.
"""

from __future__ import annotations

import datetime
import json
import sys

import requests

from crewaimeat.generator_tool import _discover_owner, _token

AGENT = "news-fetcher"
WARN_BYTES = 800 * 1024
MIN_CATEGORIES = 12
EXPECTED_STEPS = ("fetch", "writeA", "writeB", "spaceWeather", "features", "editorial")

_PASS, _FAIL = "PASS", "FAIL"
_verdicts: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str) -> bool:
    _verdicts.append((_PASS if ok else _FAIL, name, detail))
    print(f"[{_PASS if ok else _FAIL}] {name}: {detail}")
    return ok


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()
    edition = sys.argv[2] if len(sys.argv) > 2 else "evening"
    tok, url = _token(AGENT, _discover_owner(AGENT))
    if not tok:
        print(f"no token for {AGENT} — is it registered and approved?", file=sys.stderr)
        return 2
    base, headers = url.rstrip("/"), {"Authorization": f"Bearer {tok}"}

    def get(path: str) -> dict:
        r = requests.get(f"{base}{path}", headers=headers, timeout=60)
        try:
            return r.json().get("data") or {}
        except ValueError:
            return {}

    print(f"— (L)AIMEAT Sanomat {date} {edition} —\n")

    items = get(f"/v1/memory?owner_scope=true&prefix=news.{date}.&include=meta").get("items") or []
    keys = [it.get("key", "") for it in items]
    raw_keys = [k for k in keys if k.endswith(".raw")]
    legacy = [k for k in keys if ".raw." in k and not k.endswith(".raw.lukijoilta")]

    check("key count", len(keys) <= 30, f"{len(keys)} keys (was 44 per run; target ~25)")
    check("exactly one raw key", len(raw_keys) == 1, f"{len(raw_keys)} key(s) ending in '.raw': {raw_keys}")
    check(
        "no leftover per-category raw",
        not legacy,
        "none" if not legacy else f"{len(legacy)} still present: {legacy[:5]}",
    )

    if raw_keys:
        raw = get(f"/v1/memory/{raw_keys[0]}?owner_scope=true").get("value")
        if isinstance(raw, str):
            raw = json.loads(raw)
        cats = (raw or {}).get("categories") or {}
        n = sum(1 for v in cats.values() if v)
        size = len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
        check("raw categories", n >= MIN_CATEGORIES, f"{n} non-empty (need >= {MIN_CATEGORIES})")
        check("raw size", size < WARN_BYTES, f"{size / 1024:.0f} kB (floor {WARN_BYTES // 1024} kB, cap 1024 kB)")

    # THE TRAP: six agents patching without owner_scope produce six records, one per agent namespace,
    # every write returning ok. So this checks the COUNT and the owner_gaii, not just the fields.
    st_key = f"news.{date}.{edition}.status"
    st_rows = [it for it in items if it.get("key") == st_key]
    owners = {it.get("owner_gaii") for it in st_rows}
    check(
        "status is ONE record",
        len(st_rows) == 1,
        f"{len(st_rows)} record(s) under {owners or '—'} (six would mean owner_scope was dropped)",
    )
    if owners:
        gaii = next(iter(owners)) or ""
        check("status is owner-held", "#" not in gaii, f"owner_gaii={gaii} (an agent GAII contains '#')")
    status = get(f"/v1/memory/{st_key}?owner_scope=true").get("value") or {}
    if isinstance(status, str):
        status = json.loads(status)
    not_done = [s for s in EXPECTED_STEPS if status.get(s) != "done"]
    check(
        "all six steps done",
        not not_done,
        "all done" if not not_done else f"{ {s: status.get(s) for s in not_done} }",
    )

    # The signals the workflow itself gates on, evaluated the same way the node evaluates them.
    # A DIRECT-REST lister is injected for the same reason the rest of this script uses one: the
    # default reader goes through the connector, which returns empty lists off-fleet — and an empty
    # list makes every signal look legitimately red.
    from crewaimeat.workflow_spec import check_workflow

    def lister(prefix: str) -> list[dict]:
        rows = get(f"/v1/memory?owner_scope=true&prefix={prefix}&limit=500").get("items") or []
        out = []
        for it in rows:
            v = it.get("value")
            if v is None:
                v = get(f"/v1/memory/{it.get('key')}?owner_scope=true").get("value")
            out.append({"key": it.get("key"), "value": v})
        return out

    res = check_workflow("laimeat-sanomat-evening", {"date": date, "edition": edition}, lister=lister)
    for s in res["steps"]:
        check(
            f"step {s['id']}",
            s["state"] == "GREEN",
            f"{s['state']} — in: {s['input']['observed']} | out: {s['output']['observed']}",
        )

    failed = [v for v in _verdicts if v[0] == _FAIL]
    print(f"\n{len(_verdicts) - len(failed)}/{len(_verdicts)} checks passed.")
    if failed:
        print("FAILED: " + "; ".join(n for _, n, _ in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
