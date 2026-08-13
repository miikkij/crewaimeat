"""Re-run one whole Sanomat edition, in the workflow's own order.

    uv run python scripts/rerun_edition.py [YYYY-MM-DD] [evening]

Calls the same stage functions the workflow dispatches, so a re-run exercises the real pipeline
rather than a parallel copy of it: fetch -> (Desk A ‖ Desk B) -> space weather -> quiz -> editorial.
The two desks run concurrently because they do in the workflow, and running them in series would
double the wall-clock for no reason.

WHY A RE-FETCH AND NOT JUST A REWRITE. The point of a re-run after a fetch-side fix is the raw:
sources now carry `published_at` and provably stale ones are dropped. Rewriting from the old raw
would reproduce the same articles from the same undated soup.

ONE THING TO EXPECT. `_recent_seen_urls` excludes URLs used in the last few editions — including
the edition being replaced. So a re-fetch deliberately looks for DIFFERENT sources than the run it
replaces, and a category can come back thinner. That is the dedup working, not a failure; the
per-category report shows exactly what each one found.
"""

from __future__ import annotations

import datetime
import sys
import threading
import time

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, "src")

from crewaimeat.env_guard import load_env  # noqa: E402

load_env()

from crewaimeat.editorial_pipeline import build_editorial_and_index  # noqa: E402
from crewaimeat.features_pipeline import build_quiz  # noqa: E402
from crewaimeat.fetch_pipeline import build_edition_raw  # noqa: E402
from crewaimeat.space_weather_pipeline import write_space_weather  # noqa: E402
from crewaimeat.write_pipeline import DESK_A, DESK_B, WriteIncomplete, write_edition_articles  # noqa: E402


def _step(label: str, fn):
    t0 = time.time()
    print(f"\n=== {label} ===", flush=True)
    try:
        out = fn()
    except WriteIncomplete as exc:  # a partial desk reports itself; the rest of the edition goes on
        out = f"{exc.report}\nINCOMPLETE: {exc}"
    except Exception as exc:  # noqa: BLE001 — one failed stage must not abort the others
        out = f"FAILED: {type(exc).__name__}: {exc}"
    print(f"{out}\n[{label}: {time.time() - t0:.0f}s]", flush=True)
    return out


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()
    edition = sys.argv[2] if len(sys.argv) > 2 else "evening"
    print(f"RE-RUN {date} {edition}", flush=True)

    _step("fetch", lambda: build_edition_raw("news-fetcher", date, edition))

    # Desks in parallel, as the workflow runs them (after: [fetch] on both).
    results: dict[str, str] = {}

    def desk(agent: str, cats: list[str], field: str):
        results[field] = _step(field, lambda: write_edition_articles(agent, date, edition, cats, status_step=field))

    threads = [
        threading.Thread(target=desk, args=("news-writer", DESK_A, "writeA"), daemon=True),
        threading.Thread(target=desk, args=("news-writer-b", DESK_B, "writeB"), daemon=True),
    ]
    for t in threads:
        t.start()
    _step("space-weather", lambda: write_space_weather("space-weather-writer", date, edition))
    for t in threads:
        t.join()

    _step("features", lambda: build_quiz("daily-features-writer", date, edition))
    _step("editorial", lambda: build_editorial_and_index("editorial-writer", date, edition))
    print("\nRE-RUN VALMIS — aja scripts/verify_edition_consolidation.py", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
