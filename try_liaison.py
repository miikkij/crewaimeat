"""Kertakäyttöinen koe: AIMEAT Liaison Agent -pattern crewfive-LLM:llä.

Ajaa liaison-agentin, joka hoitaa Hello Integrationin company-crew-identiteetillä
AIMEAT-noden MCP-pinnan kautta (stdio: spawnaa `aimeat connect serve --agent company-crew`).
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()
for s in (sys.stdout, sys.stderr):
    r = getattr(s, "reconfigure", None)
    if r:
        r(encoding="utf-8")

import os  # noqa: E402

from crewai import Crew, Task  # noqa: E402
from aimeat_crewai import create_liaison_agent, stdio_params  # noqa: E402
from crewfive.llm import get_llm  # noqa: E402

llm = get_llm()

# Windows-workaround: stdio_params(command="aimeat") kaatuu WinError 193:een,
# koska npm-asennettu `aimeat` on .cmd-shim jota mcp:n stdio_client ei osaa
# CreateProcessata suoraan. Ajetaan se cmd.exe:n kautta.
if os.name == "nt":
    from mcp import StdioServerParameters

    params = StdioServerParameters(
        command="cmd",
        args=["/c", "aimeat", "connect", "serve", "--agent", "company-crew"],
    )
else:
    params = stdio_params(agent_name="company-crew")

with create_liaison_agent(mcp_server_params=params, llm=llm, verbose=True) as liaison:
    # Listaa mitä MCP-työkaluja liaison sai (vastaa kysymykseen tool_filteristä)
    tool_names = [t.name for t in liaison.tools]
    print("=== LIAISON TOOLS (" + str(len(tool_names)) + ") ===", file=sys.stderr)
    print(", ".join(sorted(tool_names)), file=sys.stderr)

    onboard = Task(
        description=(
            "Complete AIMEAT Hello Integration for this crew. "
            "Call aimeat_onboarding_status first. Finish any missing onboarding "
            "steps in the correct order (identify_platform with platform='crewai', "
            "confirm_skill_installed, capabilities_report, memory_write for "
            "publish_config, confirm_directives_read). Then use aimeat_task_list to "
            "find the queued 'Onboarding verification' test task and complete it via "
            "aimeat_task_complete with a short summary. Finally call "
            "aimeat_onboarding_status again and report the final state."
        ),
        expected_output=(
            "A short report listing which onboarding steps are now 'passed' and "
            "confirmation that the test task was completed."
        ),
        agent=liaison,
    )

    crew = Crew(agents=[liaison], tasks=[onboard], verbose=True)
    result = crew.kickoff()

    print("\n=== LIAISON RESULT ===")
    print(result.raw)
