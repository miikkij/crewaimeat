"""Daily reset — wipe one edition and produce it again from scratch.

    uv run python scripts/daily_reset.py                      # DRY RUN for today, shows the plan
    uv run python scripts/daily_reset.py --yes                # wipe today, then re-run it
    uv run python scripts/daily_reset.py 2026-08-13 --yes     # a specific day
    uv run python scripts/daily_reset.py --yes --wipe-only    # wipe, do not re-run

A dry run is the DEFAULT: it prints exactly which keys would go and which would be kept, and
changes nothing. `--yes` is the only thing that deletes.

READER TIPS ARE KEPT unless you pass --include-tips. `news.<date>.<edition>.raw.lukijoilta` holds
material a person sent to the Sanomat desk; no re-fetch can bring it back, and losing it to save
re-scraping a news site is not a trade worth making silently.

After the wipe this runs the same stages the workflow does (scripts/rerun_edition.py), so the day
is rebuilt by the real pipeline: fetch -> desks in parallel -> space weather -> quiz -> editorial.
"""

from __future__ import annotations

import datetime
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, "src")

from crewaimeat.env_guard import load_env  # noqa: E402

load_env()

from crewaimeat.edition_reset import plan, reset  # noqa: E402


def main() -> int:
    args = list(sys.argv[1:])
    yes = "--yes" in args
    wipe_only = "--wipe-only" in args
    keep_tips = "--include-tips" not in args
    positional = [a for a in args if not a.startswith("--")]
    date = positional[0] if positional else datetime.date.today().isoformat()
    edition = positional[1] if len(positional) > 1 else "evening"

    p = plan(date, edition, keep_tips=keep_tips)
    print(f"=== {date} {edition} ===")
    print(f"POISTETTAISIIN ({len(p['delete'])}):")
    for k in p["delete"]:
        print(f"   - {k}")
    print(f"SÄILYTETÄÄN ({len(p['keep'])}):")
    for k in p["keep"]:
        print(f"   = {k}")
    if not p["delete"]:
        print("\nEi mitään poistettavaa.")

    if not yes:
        print("\nKUIVA-AJO. Mitään ei poistettu. Aja --yes kun tämä näyttää oikealta.")
        return 0

    r = reset(date, edition, confirm=True, keep_tips=keep_tips)
    if r["failed"]:
        print(f"\nVAROITUS: {len(r['failed'])} avainta jäi poistamatta — painos on sekatilassa.", file=sys.stderr)
        return 1
    print(f"\nPoistettu {len(r['deleted'])} avainta.")

    if wipe_only:
        print("--wipe-only: ei ajeta uudelleen.")
        return 0
    print("\n=== tuotetaan päivä uudelleen ===\n", flush=True)
    return subprocess.call([sys.executable, "scripts/rerun_edition.py", date, edition])


if __name__ == "__main__":
    raise SystemExit(main())
