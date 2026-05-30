"""Hierarchical C-suite crew.

The CEO acts as the manager (manager_agent) and delegates tasks to the four
department heads (CTO, CMO, CFO, COO). The roles and tasks are read from the
YAML files in the config/ folder.
"""

from __future__ import annotations

import os

from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task

from crewfive.llm import get_llm


def _web_tools() -> list:
    """Return the Tavily web search tool in a list, if TAVILY_API_KEY is set.

    If the key is missing, the agents work without web search (empty list).
    """
    if not os.getenv("TAVILY_API_KEY"):
        return []
    # Import only here so a missing tavily-python does not crash the import.
    from crewai_tools import TavilySearchTool

    return [TavilySearchTool()]


@CrewBase
class CrewFive:
    """A 5-agent executive team in a hierarchical process."""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(self, verbose: bool | None = None) -> None:
        self.llm = get_llm()
        self.tools = _web_tools()
        # Verbose: the local CLI wants visibility (default True), but the
        # AIMEAT runner runs quiet (CREW_VERBOSE=0) so stdout stays clean.
        if verbose is None:
            verbose = os.getenv("CREW_VERBOSE", "1").lower() not in ("0", "false", "")
        self.verbose = verbose

    # ---- Manager (NO @agent decorator -> not in the agents list) ----
    def manager(self) -> Agent:
        """CEO – delegates and assembles the final result."""
        return Agent(
            config=self.agents_config["ceo"],
            llm=self.llm,
            allow_delegation=True,
            verbose=self.verbose,
        )

    # ---- Workers (department heads) --------------------------------------
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

    # ---- Task (not bound to an agent -> the manager delegates) -----------
    @task
    def company_directive(self) -> Task:
        return Task(config=self.tasks_config["company_directive"])

    # ---- Crew ------------------------------------------------------------
    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=self.agents,  # @agent-decorated are collected automatically
            tasks=self.tasks,  # @task-decorated are collected automatically
            process=Process.hierarchical,
            manager_agent=self.manager(),
            verbose=self.verbose,
        )
