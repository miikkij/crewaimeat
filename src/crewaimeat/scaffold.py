"""`crewaimeat new-crew <name>` — scaffold a new AIMEAT crew from the template.

Copies the blank template (crewaimeat/templates/example_crew.py) into your current
directory as `<name>_crew.py`, sets AGENT_NAME, and prints the next steps
(register on AIMEAT, set up .env, edit build_domain, run).

The generated file imports the locked scaffold from the installed `crewaimeat`
package, so you only ever edit your own `build_domain`.
"""

from __future__ import annotations

import re
import sys
from importlib import resources
from pathlib import Path

_TEMPLATE_PKG = "crewaimeat.templates"
_TEMPLATE_FILE = "example_crew.py"


def _usage() -> str:
    return (
        "Usage:\n"
        "  crewaimeat new-crew <agent-name>       scaffold a new crew from the template\n"
        "  crewaimeat try <def.json> --prompt X   run a JSON crew def ONCE, locally — registers nothing\n"
        "  crewaimeat doctor [--live] [--strict]  reconcile registries + routes (and the node)\n"
        "  crewaimeat retire <agent> [--apply]    stop an agent participating (the opposite of forging one)\n"
        "  crewaimeat costs [--days N]            model spend per agent + is the routing still priced right\n"
        "  crewaimeat costs --prices              only the price check (needs no node token)\n"
        "  crewaimeat quality [--days N]          published-article grounding + completeness, by MODEL\n"
        "  crewaimeat orphans [--apply]           agents the NODE holds that no crew file backs\n\n"
        "Examples:\n"
        "  crewaimeat new-crew support-bot\n"
        "  -> creates ./support_bot_crew.py for the AIMEAT agent 'support-bot'.\n"
        "  crewaimeat doctor --strict\n"
        "  -> what CI runs: every registry must agree and every route must be sanctioned.\n"
        "  crewaimeat quality --days 21\n"
        "  -> did the paper get better when the model changed? Attributed per article, not per date.\n"
        "  crewaimeat costs --prices\n"
        "  -> does any chain reach an expensive pinned model before a cheaper equal? A pin that was\n"
        "     2.7x CHEAPER when it was chosen was 4.2x DEARER eleven days later, and nothing went red.\n"
        "  crewaimeat try crew_defs/joker.json --prompt 'kissoista'\n"
        "  -> the bench: validate + one real run. No registration, no fleet restart, nothing to clean\n"
        "     up. Add --as <registered-agent> when the def uses node tools and its own name has no\n"
        "     token yet — the crew stays the doc's, only the credentials are borrowed."
    )


def _read_template() -> str:
    return resources.files(_TEMPLATE_PKG).joinpath(_TEMPLATE_FILE).read_text(encoding="utf-8")


def _next_steps(name: str, rel: str) -> str:
    from crewaimeat.forge import AIMEAT_CONNECTOR as connector  # ONE source for the pinned version

    return f"""\
Created {rel}  (AIMEAT agent: '{name}')

Next steps
──────────
1. Register the agent on AIMEAT, then approve it in the dashboard (Profile → Agents):
     npx {connector} connect --url https://aimeat.io --owner <your-aimeat-account> --agent {name}
   (<your-aimeat-account> is the AIMEAT username you sign in with — the agent's owner.)

2. Create .env from the template and add your keys:
     copy .env.example .env      (Windows)    |    cp .env.example .env   (macOS/Linux)
     OPENROUTER_API_KEY=...                  # get one at https://openrouter.ai/keys
     OPENROUTER_MODEL=openrouter/owl-alpha   # free, ideal for testing; pick a paid model for speed + quality
     TAVILY_API_KEY=...                      # optional, adds web search for agents that use it

3. Define your crew — this is the only file you edit:
     open {rel} and fill in build_domain() with your agents and their tasks.
     The scaffold already provides everything AIMEAT-related, so build_domain is all you write.
     (Background: SCAFFOLD_CANON.md.  Worked example: crewaimeat/research_crew.py.)

   Fastest path — let an AI assistant build it for you. In Claude Code / Copilot
   (with this folder open), paste:

     Read CREW_AUTHORING_PROMPT.md and let's build {rel} together.

4. Start the crew:
     • One test run:     uv run python {rel}
     • Keep it running:  ./scripts/watchdog.ps1 {rel}      (Windows)
                         ./scripts/watchdog.sh  {rel}      (macOS/Linux)
   The crew completes Hello Integration once, then every ~30s it checks AIMEAT for
   queued tasks and runs them. The watchdog keeps it alive across restarts and, if the
   agent can no longer authenticate, points you back to the dashboard to re-approve it.

5. Queue a task for '{name}' from the AIMEAT dashboard (its Tasks tab → + New Task)
   and watch it run: live status appears under the memory key
   agents.{name}.tasks.<id>.live, and the deliverable lands in memory when done.
"""


def _new_crew(name: str) -> int:
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        print(
            f"error: invalid agent name {name!r}. Use letters, digits, '.', '_' or '-'.",
            file=sys.stderr,
        )
        return 1

    fname = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") + "_crew.py"
    crews_dir = Path.cwd() / "crews"
    crews_dir.mkdir(exist_ok=True)
    dest = crews_dir / fname
    rel = f"crews/{fname}"
    if dest.exists():
        print(
            f"error: {rel} already exists — pick another name, or edit the existing file.",
            file=sys.stderr,
        )
        return 1

    content = _read_template()
    # Set the agent identity and point the run hint at the generated file.
    content = content.replace('AGENT_NAME = "my-crew"', f'AGENT_NAME = "{name}"')
    content = content.replace("python -m crewaimeat.templates.example_crew", f"python {rel}")

    dest.write_text(content, encoding="utf-8")
    print(_next_steps(name, rel))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "new-crew":
        return _new_crew(argv[1])
    if argv and argv[0] == "try":
        from crewaimeat.crew_try import main as try_main

        return try_main(argv[1:])
    if argv and argv[0] == "doctor":
        from crewaimeat.doctor.cli import main as doctor_main

        return doctor_main(argv[1:])
    if argv and argv[0] == "orphans":
        from crewaimeat.node_cleanup import main as orphans_main

        return orphans_main(argv[1:])
    if argv and argv[0] == "quality":
        from crewaimeat.quality import main as quality_main

        return quality_main(argv[1:])
    if argv and argv[0] == "costs":
        from crewaimeat.fleet_economics import main as costs_main

        return costs_main(argv[1:])
    if argv and argv[0] == "retire":
        from crewaimeat.retire import main as retire_main

        return retire_main(argv[1:])
    print(_usage())
    return 0 if (argv and argv[0] in ("-h", "--help", "help")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
