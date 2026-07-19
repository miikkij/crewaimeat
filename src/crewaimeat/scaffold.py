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
        "  crewaimeat new-crew <agent-name>\n\n"
        "Example:\n"
        "  crewaimeat new-crew support-bot\n"
        "  -> creates ./support_bot_crew.py for the AIMEAT agent 'support-bot'."
    )


def _read_template() -> str:
    return resources.files(_TEMPLATE_PKG).joinpath(_TEMPLATE_FILE).read_text(encoding="utf-8")


def _next_steps(name: str, rel: str) -> str:
    return f"""\
Created {rel}  (AIMEAT agent: '{name}')

Next steps
──────────
1. Register the agent on AIMEAT, then approve it in the dashboard (Profile → Agents):
     npx aimeat@2.0.0 connect --url https://aimeat.io --owner <your-aimeat-account> --agent {name}
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
    print(_usage())
    return 0 if (argv and argv[0] in ("-h", "--help", "help")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
