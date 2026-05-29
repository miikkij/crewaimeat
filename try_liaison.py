"""Kanoninen AIMEAT Liaison Agent -kruu (aimeat-crewai 0.2.2, Step 3 -mallin mukaan).

Liaison + domain-agentit (researcher, writer). Liaison hoitaa Hello Integrationin
ja kirjoittaa lopputuloksen AIMEATin muistiin. Domain-agentit eivät tiedä AIMEATista.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()
for s in (sys.stdout, sys.stderr):
    r = getattr(s, "reconfigure", None)
    if r:
        r(encoding="utf-8")

from crewai import Agent, Crew, Task  # noqa: E402
from aimeat_crewai import create_liaison_agent, stdio_params  # noqa: E402

from crewfive.crew import _web_tools  # noqa: E402
from crewfive.llm import get_llm  # noqa: E402

AGENT_NAME = "demo-crew"
llm = get_llm()

with create_liaison_agent(
    mcp_server_params=stdio_params(agent_name=AGENT_NAME),
    agent_name=AGENT_NAME,
    llm=llm,
    verbose=True,
) as liaison:
    # 0.2.2-verifiointi
    sk = getattr(liaison, "skills", None)
    print(f"=== agent.skills: {[getattr(x,'name',x) for x in (sk or [])]} (None? {sk is None}) ===", file=sys.stderr)
    print(f"=== backstory length: {len(liaison.backstory or '')} chars ===", file=sys.stderr)

    researcher = Agent(
        role="Researcher",
        goal="Find concrete, current facts on the requested topic",
        backstory="Perusteellinen tutkija joka käyttää web-hakua faktojen varmistamiseen.",
        tools=_web_tools(),
        llm=llm,
        verbose=True,
    )
    writer = Agent(
        role="Writer",
        goal="Write a tight, useful summary from the research",
        backstory="Ammattikirjoittaja joka tiivistää olennaisen.",
        llm=llm,
        verbose=True,
    )

    crew = Crew(
        agents=[liaison, researcher, writer],
        tasks=[
            Task(
                description=(
                    "Check AIMEAT onboarding status. Complete any pending step via the "
                    "matching aimeat_onboarding_* tool. Report the final state."
                ),
                expected_output="Final onboarding state and list of passed steps.",
                agent=liaison,
            ),
            Task(
                description="Find 2 concrete facts about budgeting apps for students.",
                expected_output="Two concrete facts with brief sources.",
                agent=researcher,
            ),
            Task(
                description="Write a 2-sentence summary from the research findings.",
                expected_output="A 2-sentence summary.",
                agent=writer,
            ),
            Task(
                description=(
                    "Write the final crew output (the writer's summary) to AIMEAT memory "
                    f"under the key 'demo.{AGENT_NAME}.latest_output' with visibility owner."
                ),
                expected_output="Confirmation of the memory write (key + status).",
                agent=liaison,
            ),
        ],
    )

    result = crew.kickoff()
    print("\n=== CREW RESULT ===")
    print(result.raw if hasattr(result, "raw") else result)
