"""image-scout: turns image briefs into curated moodboards (internal reference use).

A general-purpose workspace-contract agent. Reads `moodboard-request` records (brief, n_images,
mode), searches the open web for images (SearXNG), analyses each with a vision model (subject,
style, colors, tags, relevance to the brief), uploads the best to agent storage (public-visibility
keys so they render for every workspace viewer) and writes a `moodboard` DOCUMENT — a gallery with
the images embedded plus their metadata and source links. The loop is deterministic
(crewaimeat.image_contract); the LLM only looks at pictures (qwen-vl). It posts nothing external;
everything it gathers is for internal reference.

Quick test (after registering):
  uv run python -c "from crewaimeat.image_contract import process_moodboards; print(process_moodboards())"

Run as a crew:
  npx aimeat@latest connect add --agent image-scout --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/image_scout_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.image_contract import make_image_tools, process_moodboards

AGENT_NAME = "image-scout"

README = '''[[FIGLET:slant]["Image Scout"]]

Turns an image **brief** into a curated **moodboard**: searches the open web (SearXNG images),
looks at every candidate with a **vision model** (subject, style, colors, tags, relevance),
uploads the best to storage and writes a `moodboard` **document** with the images embedded +
metadata + source links. Reads `moodboard-request` records; `mode: upload-only` skips the
document and just stores the images. Deterministic loop; **internal reference use only — it
posts nothing external.**

**How to task me:** "scout" — I run process_moodboards ONCE and fulfil any pending requests.
'''


def build_domain(ctx: BuildContext):
    scout = Agent(
        role="Image Scout",
        goal="Turn pending moodboard-requests into moodboard documents — searched, vision-curated, sourced.",
        backstory="You fulfil image briefs: search the web for candidate images, analyse each with "
                  "a vision model, keep the most relevant, store them and write a moodboard document "
                  "with metadata and source links. You call process_moodboards ONCE and report. "
                  "Everything is internal reference material; you never post anywhere external.",
        llm=ctx.llm,
        tools=[*make_image_tools(AGENT_NAME)],
    )

    scout_task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "Call process_moodboards() EXACTLY ONCE. It deterministically finds pending "
            "moodboard-request records, searches + vision-curates + uploads images for each, writes "
            "the moodboard document and advances the request. Report the counts. Post NOTHING."
        ),
        agent=scout,
        expected_output="The process_moodboards report: how many moodboard requests were fulfilled.",
    )

    return ([scout], [scout_task])


def run() -> None:
    # idle_hook: a DETERMINISTIC poll that fulfils any pending moodboard-requests. The CHECK uses
    # NO LLM (workspace reads + delta math); the vision model runs only on real candidate images.
    def _poll() -> None:
        res = process_moodboards()
        if res.get("processed") or res.get("failed"):
            print(f"[{AGENT_NAME}] moodboard poll: {res}")

    run_crew(CrewSpec(
        agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README,
        temperature=0.4, idle_hook=_poll, idle_hook_seconds=300,
    ))


if __name__ == "__main__":
    run()
