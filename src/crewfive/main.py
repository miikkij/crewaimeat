"""CLI entrypoint.

Usage:
    uv run crew "Task description here"
    uv run crew                # uses the default demo

Runs the hierarchical crew and saves the results to the output/ folder
(both a readable .md and a machine-readable .json with a timestamp).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_REQUEST = (
    "Draft a business plan for a new B2B SaaS product that helps SMEs automate "
    "their invoicing with AI. Cover technology, marketing, finance and operations."
)

# Results are saved to the output/ folder in the project root.
OUTPUT_DIR = Path(__file__).resolve().parents[2] / "output"


def run() -> None:
    # Force UTF-8 on stdout: the Windows console (cp1252) otherwise cannot print
    # accented characters/emojis, and CrewAI's event handler crashes on them.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    load_dotenv()

    request = " ".join(sys.argv[1:]).strip() or DEFAULT_REQUEST

    # Import only after load_dotenv() so the LLM picks up keys from the env.
    from crewfive.crew import CrewFive

    print(f"\n=== crewfive ===\nDirective: {request}\n")

    crew = CrewFive().crew()
    result = crew.kickoff(inputs={"request": request})

    md_path, json_path = _save_outputs(request, result)

    print("\n" + "=" * 70)
    print("FINAL REPORT:\n")
    print(result.raw)
    print("=" * 70)
    print(f"\nSaved:\n  {md_path}\n  {json_path}")


def _save_outputs(request: str, result) -> tuple[Path, Path]:
    """Save the final result and each task's output to disk."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # --- Readable Markdown report ---
    md_path = OUTPUT_DIR / f"report_{stamp}.md"
    md = [
        f"# Executive report\n",
        f"**Timestamp:** {datetime.now().isoformat(timespec='seconds')}  ",
        f"\n**Directive:** {request}\n",
        "\n---\n",
        result.raw or "(no output)",
    ]
    md_path.write_text("\n".join(md), encoding="utf-8")

    # --- Machine-readable JSON (final result + per-task outputs + tokens) ---
    tasks_output = []
    for t in getattr(result, "tasks_output", []) or []:
        tasks_output.append(
            {
                "name": getattr(t, "name", None),
                "description": getattr(t, "description", None),
                "agent": getattr(t, "agent", None),
                "raw": getattr(t, "raw", None),
            }
        )

    token_usage = getattr(result, "token_usage", None)
    try:
        token_usage = token_usage.model_dump() if token_usage is not None else None
    except Exception:
        token_usage = str(token_usage) if token_usage is not None else None

    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "request": request,
        "final_output": result.raw,
        "tasks_output": tasks_output,
        "token_usage": token_usage,
    }
    json_path = OUTPUT_DIR / f"report_{stamp}.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return md_path, json_path


if __name__ == "__main__":
    run()
