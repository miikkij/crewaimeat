"""datapkg-analyst — answers a question about an AIMEAT data package, from its SCHEMA.

The shape, and why it is this small: the agent is given an address and a question, and it has five
tools. The one that matters is `open_package`, which hands back the Table Schema — every column's
name and type. There is no data dictionary and no tool that could return one. If the agent still
cannot proceed, the schema was not enough, and that is the finding we are here to produce.

The model never sees the rows. It names an aggregation (`aggregate`) and deterministic code runs it
on the typed dataframe. Two reasons, and neither is prompt-frugality: 718 rows in a context window
is a model doing arithmetic by eye, and the types are the whole point of the package — the moment
the rows become text in a prompt, `00123` is a number again and the schema was for nothing.
"""

from __future__ import annotations

import json

from crewai import Agent, Task
from crewai.tools import tool

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.datapkg import QualityGateRefused, aggregate, open_package, publish, save_parquet, versions

AGENT_NAME = "datapkg-analyst"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises. These used to live in three central lists (fleet_identity.py / llm_providers.json /
# offers.py) that nothing kept in step, so an agent could — and did — come online missing from
# all of them. crewaimeat.agent_manifest reads these statically; the lists are derived.
LLM_PROFILE = "coding"
TAGS = ["data-packages", "frictionless", "tabular-analysis", "role.task-runner"]
CAPABILITIES = {
    "technical": [
        {"name": "datapkg-analyst", "type": "skill"},
        {"name": "aimeat_datapackage_publish", "type": "tool"},
        {"name": "aimeat_datapackage_export", "type": "tool"},
    ],
    "domain": [
        "answering questions about a published dataset from its schema",
        "publishing a versioned data package with a real changes note",
        "pinning a version by its permanent content-hash address",
        "frictionless table schema",
        "pandas + parquet tabular analysis",
    ],
    "languages": ["fi", "en"],
}


README = """Reads published AIMEAT data packages and answers questions about them from their
Frictionless Table Schema — column names and types — rather than from documentation. Types come
from the schema, never from inference, so zero-padded identifiers and dates survive. Can publish a
new version (writing a real `changes` sentence), list versions, pin an older one by its permanent
content-hash address, and hand a package on as Parquet."""


def _tools() -> list:
    @tool("open_package")
    def _open(url: str) -> str:
        """Open a data package and return its SCHEMA: every column's name and type, the row count,
        the licence and the permanent address. Accepts either the /v1/datapackages/<owner>/<name>
        address or a descriptor URL. Call this FIRST — the column names you need are in the reply."""
        return json.dumps(open_package(url), ensure_ascii=False)

    @tool("aggregate")
    def _agg(url: str, value: str, how: str = "mean", group_by: str = "", where: str = "") -> str:
        """Run ONE typed aggregation. `value` is the column to aggregate, `how` is mean/sum/min/max/
        count, `group_by` an optional column, `where` an optional filter like "stillRunning == True".
        Column names come from open_package. Types are the schema's, so a date compares as a date."""
        try:
            return json.dumps(aggregate(url, value, how, group_by or None, where or None), ensure_ascii=False)
        except KeyError as exc:  # the schema already answers this — hand the model the real names
            return f"UNKNOWN COLUMN: {exc}"

    @tool("package_versions")
    def _vers(owner: str, name: str) -> str:
        """Every version of a package, newest first, each with its contentHash, changes sentence and
        descriptorUrl. The descriptorUrl of an older entry is how you pin it: it keeps returning the
        old content after a newer version exists."""
        return json.dumps(versions(owner, name, agent=AGENT_NAME), ensure_ascii=False)[:4000]

    @tool("save_parquet")
    def _pq(url: str, path: str) -> str:
        """Write the package to `path` as Parquet, types intact. Raises when pyarrow is missing
        rather than quietly writing a CSV."""
        return f"wrote {save_parquet(url, path)} to {path}"

    @tool("publish_version")
    def _pub(name: str, rows_json: str, changes: str, declared_schema: str = "") -> str:
        """Publish a new version. `changes` must say what actually differs — a stranger decides from
        that sentence alone whether to move to this version, so "update" is refused. Declare
        `declared_schema` to make the quality gate bite: an undeclared schema widens to fit whatever
        arrived and cannot fail."""
        kw = {"schema": json.loads(declared_schema)} if declared_schema else {}
        try:
            return json.dumps(publish(name, json.loads(rows_json), changes, agent=AGENT_NAME, **kw), ensure_ascii=False)
        except QualityGateRefused as exc:
            # The rows and fields live on .issues; str(exc) only counts them. Surfacing both is the
            # difference between "2 problems" and something the caller can fix.
            return f"REFUSED — nothing was written, the package still stands on its previous version.\n{exc}\nissues: {json.dumps(exc.issues, ensure_ascii=False)}"

    return [_open, _agg, _vers, _pq, _pub]


def build_domain(ctx: BuildContext):
    analyst = Agent(
        role="Data Package Analyst",
        goal="Answer the question asked about a published data package, using its schema.",
        backstory=(
            "You read AIMEAT data packages. You always call open_package first: its reply carries "
            "every column's name and type, which is the only column information that exists and the "
            "only kind you need. You never ask for a data dictionary and never guess a column name — "
            "if the schema does not answer the question, you say exactly what it failed to tell you. "
            "You do not read rows into your answer; you name an aggregation and let it run typed."
        ),
        llm=ctx.llm,
        tools=_tools(),
        max_iter=12,
    )
    task = Task(
        description=(
            f"Today is {ctx.today}.\n\nRequest: '{ctx.prompt}'\n\n"
            "Open the package named in the request, read its schema, and answer the question from "
            "the data. State the column names you used and where the numbers came from. If the "
            "schema cannot answer it, say which column would have been needed."
        ),
        agent=analyst,
        expected_output="The answer, the columns it came from, and the package address it was read from.",
    )
    return ([analyst], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.2))


if __name__ == "__main__":
    run()
