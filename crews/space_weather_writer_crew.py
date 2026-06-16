"""space-weather-writer: deterministic 'Avaruussää tänään' article for (L)AIMEAT Sanomat.

The category fetch + write run in code (crewaimeat.space_weather_pipeline): NOAA SWPC + spaceweather.com
are fetched, grok writes ONE original Finnish article (aurora chances, solar activity), and it is stored
under news.<date>.<edition>.article.avaruussaa. The old crew let the LLM guess the edition from the prompt
and defaulted to 'am' when it couldn't — so a scheduled 'evening' run wrote the wrong key and the workflow's
evening signal went RED. This crew is a thin wrapper: the agent resolves the target date+edition and calls
write_space_weather ONCE; the edition is then used verbatim. The sky-panel images (auroral oval / coronal
holes / daily sun) are hotlinked live in the app itself — this crew writes the words. Sources are
public-domain (NOAA) / freely readable (spaceweather.com); we reproduce facts, not text.

Register + approve before running:
  npx aimeat@latest connect add --agent space-weather-writer --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>
Run: uv run python crews/space_weather_writer_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.space_weather_pipeline import make_space_weather_tools

AGENT_NAME = "space-weather-writer"

README = '''[[FIGLET:slant]["Space Weather"]]

Reads NOAA SWPC + spaceweather.com and writes one Finnish "Avaruussää tänään" article (revontulet,
auringon aktiivisuus) into the newspaper — deterministic fetch+write loop, grok prose, in Tähti Sointu's voice.

**How to task me:** "Kirjoita tämän illan avaruussää-artikkeli editioon <morning/evening>."
'''


def build_domain(ctx: BuildContext):
    writer = Agent(
        role="Space-Weather Write Runner",
        goal="Resolve the target date + edition from the request and trigger the deterministic avaruussää write.",
        backstory="You do not write the article by hand. You read the request, work out the target date and "
                  "edition, and call write_space_weather ONCE — the tool fetches NOAA/spaceweather.com and grok "
                  "writes one Finnish 'Avaruussää tänään' article in Tähti Sointu's voice. You then report the key.",
        llm=ctx.llm,
        tools=[*make_space_weather_tools(AGENT_NAME)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "1. Resolve the TARGET DATE (YYYY-MM-DD — the date in the request, else today) and EDITION "
            "('morning' if the request mentions aamu/morning, else 'evening' — evening is the scheduled default).\n"
            "2. Call write_space_weather(date=<resolved>, edition=<resolved>) EXACTLY ONCE. It fetches the "
            "sources and writes the article — you do NOT write it yourself.\n"
            "3. Return the key + char-count report it gives you."
        ),
        agent=writer,
        expected_output="The write_space_weather report: the avaruussaa key written + char count.",
    )
    return ([writer], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.6))


if __name__ == "__main__":
    run()
