"""space-weather-writer: generated on the AIMEAT scaffold (crewaimeat).

Fetches public space-weather data (NOAA SWPC 3-day forecast text + spaceweather.com narrative) and
writes ONE original Finnish "Avaruussää tänään" article (aurora chances, solar activity), publishing it
to the newspaper's article memory so it appears in (L)AIMEAT Sanomat. The sky-panel images
(auroral oval / coronal holes / daily sun) are hotlinked live in the app itself — this crew writes the
words. Sources are public-domain (NOAA) / freely readable (spaceweather.com); we reproduce facts, not text.

Register + approve before running:
  npx aimeat@latest connect add --agent space-weather-writer --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>
Run: uv run python crews/space_weather_writer_crew.py
"""

from __future__ import annotations

import requests
from crewai import Agent, Task
from crewai.tools import tool

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.memory_tools import make_memory_tools

AGENT_NAME = "space-weather-writer"

README = '''[[FIGLET:slant]["Space Weather"]]

Reads NOAA SWPC + spaceweather.com and writes one Finnish "Avaruussää tänään" article (revontulet,
auringon aktiivisuus) into the newspaper.

**How to task me:** "Kirjoita tämän päivän avaruussää-artikkeli editioon <am/pm>."
'''


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@tool("fetch_space_weather")
def fetch_space_weather() -> str:
    """Fetch today's PUBLIC space-weather data: NOAA SWPC's 3-day geomagnetic/solar forecast (clean text,
    public domain) and spaceweather.com's narrative (for aurora/sunspot/CME context). Returns the combined
    raw text to base a Finnish article on. Reproduce the FACTS in your own Finnish words; do not copy text."""
    out = []
    try:
        r = requests.get("https://services.swpc.noaa.gov/text/3-day-forecast.txt", timeout=20)
        r.encoding = "utf-8"
        if r.status_code == 200:
            out.append("=== NOAA SWPC 3-DAY FORECAST (public domain) ===\n" + r.text.strip()[:3500])
    except Exception as e:  # noqa: BLE001
        out.append(f"(NOAA forecast unavailable: {e!r})")
    try:
        r = requests.get("https://spaceweather.com/", timeout=20)
        r.encoding = "utf-8"
        if r.status_code == 200:
            txt = _strip_html(r.text)
            # keep the most relevant slice (aurora / sunspot / solar wind context)
            out.append("=== spaceweather.com narrative (facts only) ===\n" + txt[:3500])
    except Exception as e:  # noqa: BLE001
        out.append(f"(spaceweather.com unavailable: {e!r})")
    return "\n\n".join(out) if out else "No space-weather sources reachable."


def build_domain(ctx: BuildContext):
    correspondent = Agent(
        role="AIMEAT Space-Weather Correspondent",
        goal=("Fetch today's public space-weather data and write ONE original, accessible Finnish article "
              "about it (revontulten todennäköisyys Suomessa, auringon aktiivisuus, magneettiset myrskyt), "
              "signed with the byline '— Tähti Sointu'."),
        backstory=("You are TÄHTI SOINTU, AIMEAT Sanomat's space-weather correspondent — you turn NOAA "
                   "forecasts and solar data into a clear, slightly wondrous Finnish read: will the revontulet "
                   "show over Suomi tonight, what is the sun doing, is a geomagnetic storm coming. You explain "
                   "Kp-indices and coronal holes plainly, never sensationalise, and reproduce facts in your own "
                   "Finnish words. You always end with the byline '— Tähti Sointu'."),
        llm=ctx.llm,
        tools=[fetch_space_weather, *make_memory_tools(AGENT_NAME)],
    )

    fetch_task = Task(
        description=(f"{ctx.today} — {ctx.prompt}\n\n"
                     "Call fetch_space_weather to get today's NOAA forecast + spaceweather.com context. "
                     "Summarise the key facts: current/forecast Kp index, any geomagnetic storm watch, solar "
                     "wind / coronal holes, sunspots / flares / CMEs, and what it means for aurora visibility "
                     "in Finland over the next 1-3 nights."),
        agent=correspondent,
        expected_output="A factual bullet summary of today's space weather and Finnish aurora outlook.",
    )

    write_task = Task(
        description=(f"{ctx.today} — Write ONE original Finnish article titled around 'Avaruussää tänään'.\n\n"
                     "INSTRUCTIONS:\n"
                     "1. Base it on the fetched facts (from context) — reproduce in your OWN Finnish words, "
                     "never copy source text.\n"
                     "2. Cover: revontulten näkyvyys Suomessa lähiöinä, auringon aktiivisuus, mahdollinen "
                     "magneettinen myrsky (Kp). Keep it clear and a little wondrous; no scare-mongering.\n"
                     "3. Determine the date + edition from the request (default: today, edition 'am'); write to "
                     "write_memory(key='news.<date>.<edition>.article.avaruussaa', value=<article>, "
                     "visibility='public').\n"
                     "4. End with the byline '— Tähti Sointu'. Report the key you wrote."),
        agent=correspondent,
        context=[fetch_task],
        expected_output="The Finnish space-weather article (signed '— Tähti Sointu') and the public memory key written.",
    )

    return ([correspondent], [fetch_task, write_task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.6))


if __name__ == "__main__":
    run()
