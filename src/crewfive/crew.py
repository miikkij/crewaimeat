"""Hierarkinen C-suite kruu.

CEO toimii managerina (manager_agent) ja delegoi taskit neljälle
osastopäällikölle (CTO, CMO, CFO, COO). Roolit ja taskit luetaan
config/-kansion YAML-tiedostoista.
"""

from __future__ import annotations

import os

from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task

from crewfive.llm import get_llm


def _web_tools() -> list:
    """Palauttaa Tavily-web-hakutyökalun listassa, jos TAVILY_API_KEY on asetettu.

    Jos avainta ei ole, agentit toimivat ilman web-hakua (lista on tyhjä).
    """
    if not os.getenv("TAVILY_API_KEY"):
        return []
    # Tuodaan vasta täällä, jotta puuttuva tavily-python ei kaada importtia.
    from crewai_tools import TavilySearchTool

    return [TavilySearchTool()]


@CrewBase
class CrewFive:
    """5 agentin johtoryhmä hierarkisessa prosessissa."""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(self, verbose: bool | None = None) -> None:
        self.llm = get_llm()
        self.tools = _web_tools()
        # Verbose: paikallinen CLI haluaa näkyvyyttä (oletus True), mutta
        # AIMEAT-runner ajaa hiljaisena (CREW_VERBOSE=0) jotta stdout pysyy puhtaana.
        if verbose is None:
            verbose = os.getenv("CREW_VERBOSE", "1").lower() not in ("0", "false", "")
        self.verbose = verbose

    # ---- Manageri (EI @agent-koristetta -> ei mukana agents-listassa) ----
    def manager(self) -> Agent:
        """CEO – delegoi ja koostaa lopputuloksen."""
        return Agent(
            config=self.agents_config["ceo"],
            llm=self.llm,
            allow_delegation=True,
            verbose=self.verbose,
        )

    # ---- Työntekijät (osastopäälliköt) -----------------------------------
    @agent
    def cto(self) -> Agent:
        return Agent(
            config=self.agents_config["cto"],
            llm=self.llm,
            tools=self.tools,
            verbose=self.verbose,
        )

    @agent
    def cmo(self) -> Agent:
        return Agent(
            config=self.agents_config["cmo"],
            llm=self.llm,
            tools=self.tools,
            verbose=self.verbose,
        )

    @agent
    def cfo(self) -> Agent:
        return Agent(
            config=self.agents_config["cfo"],
            llm=self.llm,
            tools=self.tools,
            verbose=self.verbose,
        )

    @agent
    def coo(self) -> Agent:
        return Agent(
            config=self.agents_config["coo"],
            llm=self.llm,
            tools=self.tools,
            verbose=self.verbose,
        )

    # ---- Taski (ei sidota agenttiin -> manageri delegoi) -----------------
    @task
    def company_directive(self) -> Task:
        return Task(config=self.tasks_config["company_directive"])

    # ---- Kruu ------------------------------------------------------------
    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=self.agents,  # @agent-koristellut kerätään automaattisesti
            tasks=self.tasks,  # @task-koristellut kerätään automaattisesti
            process=Process.hierarchical,
            manager_agent=self.manager(),
            verbose=self.verbose,
        )
