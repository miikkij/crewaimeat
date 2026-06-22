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

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, contract_record_spaces, record_event_targets, run_crew
from crewaimeat.contract_adopt import build_adopt_domain, is_adopt_task
from crewaimeat.image_contract import CONTRACT, make_image_tools, process_moodboards

AGENT_NAME = "image-scout"

README = """[[FIGLET:slant]["Image Scout"]]

Turns an image **brief** into a curated **moodboard**: searches the open web (SearXNG images),
looks at every candidate with a **vision model** (subject, style, colors, tags, relevance),
uploads the best to storage and writes a `moodboard` **document** with the images embedded +
metadata + source links. Reads `moodboard-request` records; `mode: upload-only` skips the
document and just stores the images. Deterministic loop; **internal reference use only — it
posts nothing external.**

**How to task me:** "scout" — I run process_moodboards ONCE and fulfil any pending requests.
"""


def build_domain(ctx: BuildContext):
    if is_adopt_task(ctx.task):  # UI "Adopt contract" chip -> provision our spaces there
        return build_adopt_domain(ctx, AGENT_NAME, CONTRACT)
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
    # Event-driven (aimeat-crewai 0.7.0): a pushed moodboard-request record (or the catch-up on connect)
    # wakes us; process_moodboards is the DETERMINISTIC scan that fulfils any pending requests (NO LLM in
    # the check; the vision model runs only on real candidate images). No idle polling. targets scopes the
    # scan to the event's OWN workspace — no member rediscovery/full re-scan per event, and loop-safe.
    def _on_record(event) -> None:
        res = process_moodboards(targets=record_event_targets(event))
        if res.get("processed") or res.get("failed"):
            print(f"[{AGENT_NAME}] moodboard event: {res}")

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.4,
            listen_for=("tasks", "records"),
            record_spaces=lambda: contract_record_spaces(AGENT_NAME, CONTRACT),
            on_record=_on_record,
        )
    )


if __name__ == "__main__":
    run()
