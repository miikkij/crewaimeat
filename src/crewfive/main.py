"""CLI-entrypoint.

Käyttö:
    uv run crew "Tehtävän kuvaus tähän"
    uv run crew                # käyttää oletusdemoa

Ajaa hierarkisen kruun ja tallentaa tulokset output/-kansioon
(sekä luettava .md että koneluettava .json aikaleimalla).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_REQUEST = (
    "Laadi liiketoimintasuunnitelma uudelle B2B SaaS -tuotteelle, joka auttaa "
    "pk-yrityksiä automatisoimaan laskutuksensa tekoälyn avulla. Kata teknologia, "
    "markkinointi, talous ja operaatiot."
)

# Tulokset tallennetaan projektin juuren output/-kansioon.
OUTPUT_DIR = Path(__file__).resolve().parents[2] / "output"


def run() -> None:
    # Pakota UTF-8 stdoutiin: Windowsin konsoli (cp1252) ei muuten osaa
    # tulostaa ääkkösiä/emojeja, ja CrewAI:n tapahtumakäsittelijä kaatuilee niihin.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    load_dotenv()

    request = " ".join(sys.argv[1:]).strip() or DEFAULT_REQUEST

    # Tuodaan vasta load_dotenv():n jälkeen, jotta LLM saa avaimet ympäristöstä.
    from crewfive.crew import CrewFive

    print(f"\n=== crewfive ===\nDirektiivi: {request}\n")

    crew = CrewFive().crew()
    result = crew.kickoff(inputs={"request": request})

    md_path, json_path = _save_outputs(request, result)

    print("\n" + "=" * 70)
    print("LOPULLINEN RAPORTTI:\n")
    print(result.raw)
    print("=" * 70)
    print(f"\nTallennettu:\n  {md_path}\n  {json_path}")


def _save_outputs(request: str, result) -> tuple[Path, Path]:
    """Tallentaa lopputuloksen ja jokaisen taskin tuotoksen levylle."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # --- Luettava Markdown-raportti ---
    md_path = OUTPUT_DIR / f"raportti_{stamp}.md"
    md = [
        f"# Johtoryhmän raportti\n",
        f"**Aikaleima:** {datetime.now().isoformat(timespec='seconds')}  ",
        f"\n**Direktiivi:** {request}\n",
        "\n---\n",
        result.raw or "(ei tulosta)",
    ]
    md_path.write_text("\n".join(md), encoding="utf-8")

    # --- Koneluettava JSON (lopputulos + per-task tuotokset + tokenit) ---
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
    json_path = OUTPUT_DIR / f"raportti_{stamp}.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return md_path, json_path


if __name__ == "__main__":
    run()
