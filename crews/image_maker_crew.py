"""image-maker: a text→image agent on the AIMEAT scaffold (crewaimeat).

Give it a task describing an image; it crafts a vivid prompt, generates the image with ByteDance
Seedream 4.5 (via OpenRouter, ~$0.04/image), stores it in public AIMEAT storage, and returns a
public URL. Only build_domain is crew-specific; crewaimeat.aimeat_crew.run_crew provides the AIMEAT
wiring. Register + approve before running:
  npx aimeat@latest connect add --agent image-maker --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>

Run: uv run python crews/image_maker_crew.py
"""

from __future__ import annotations

from crewaimeat.aimeat_crew import CrewSpec, run_crew
from crewaimeat.seedream_gen import make_image_tools

AGENT_NAME = "image-maker"

README = '''[[FIGLET:slant]["Image Maker"]]

Generates an image from your description (ByteDance Seedream 4.5) and gives you a public URL.

**How to task me:** Describe the image you want — subject, style, mood, composition (and a size/aspect
ratio if you care). I turn it into a vivid prompt, generate one image, store it publicly, and return
the link. ~$0.04 per image. I make images; I don't edit your existing files or post anywhere.
'''


def build_domain(ctx):
    from crewai import Agent, Task

    director = Agent(
        role="Image Director",
        goal=("Turn the user's request into ONE vivid, specific image-generation prompt, generate the "
              "image, and return its public URL."),
        backstory=("You are an art director who writes precise, evocative image prompts — naming the "
                   "subject, style, lighting, composition and mood — and produces a single strong image "
                   "per request rather than many rough ones."),
        llm=ctx.llm,
        tools=make_image_tools(AGENT_NAME),
    )

    make_task = Task(
        description=(
            f"{ctx.today}\n\n"
            "The user wants an image. Their request:\n\n"
            f"{ctx.prompt}\n\n"
            "Steps:\n"
            "1. Craft ONE vivid, specific image-generation prompt from the request — name the subject, "
            "style, lighting, composition and mood; keep it focused on a single image.\n"
            "2. Call `generate_image` ONCE with that prompt (choose a sensible `size` and `aspect_ratio` "
            "if the request implies them; otherwise the defaults are fine).\n"
            "3. Return the resulting public image URL together with the exact prompt you used. If "
            "generation failed, report the error verbatim — do not retry a successful one."
        ),
        agent=director,
        expected_output="The public image URL plus the prompt used (or a clear error if generation failed).",
    )

    return ([director], [make_task])


def run() -> None:
    # A creative service — a mild temperature for prompt-crafting; the image call itself is deterministic.
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.6))


if __name__ == "__main__":
    run()
