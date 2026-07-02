"""Behavioral eval for crew-forge: does it REALLY build the agent it was asked for?

Given a plain-language "order", crew-forge's Architect designs a crew and its Builder writes + validates
it. This harness runs that pipeline end-to-end in DRY-RUN mode (writes into a throwaway root, and the
register/launch side effects are neutralized — nothing ever touches aimeat.io or the live fleet) and
GRADES the generated crew against per-order expectations:

  - built:        the file compiles and build_domain returns a non-empty (agents, tasks)  [via the validator]
  - capabilities: the RIGHT tools were selected (expected ⊆ wired, and none of the forbidden ones)
  - structure:    enough agents/tasks to do the job, and the request (ctx.prompt) is actually consumed

The grader (`grade`) is pure — it inspects a generated file and needs no LLM, so it is unit-tested in
the deterministic test floor. Running the real Architect (`run_eval`) needs an LLM key + network, so it
lives behind `scripts/eval_crew_forge.py` (opt-in), not the CI test floor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from crewaimeat import forge, forge_catalog

_COUNTS_RE = re.compile(r"(\d+)\s+agents?,\s*(\d+)\s+tasks?")


@dataclass(frozen=True)
class Order:
    """One plain-language request + what a correct crew for it must look like."""

    id: str
    request: str
    expect: frozenset[str] = frozenset()  # capability ids the crew MUST wire
    forbid: frozenset[str] = frozenset()  # capability ids the crew must NOT wire
    expect_memory: bool = False  # the crew MUST enable persistent CrewAI memory (CrewSpec memory=True) —
    #   a CrewSpec-level toggle, so it is checked separately from the in-crew tool capabilities above
    min_agents: int = 1
    note: str = ""


# The corpus spans the v1 catalog: one order per capability plus a pure-reasoning order (no tools).
ORDERS: list[Order] = [
    Order(
        id="news-digest",
        request=(
            "an agent that writes a short weekly technology news digest in plain language and saves it to "
            "a PUBLIC memory key so a web app can display it"
        ),
        expect=frozenset({"memory"}),
        forbid=frozenset({"image", "app_build"}),
        note="content pipeline → writes its own-words digest to an exact public key",
    ),
    Order(
        id="web-research",
        request=(
            "an agent that researches what our competitors are doing by searching the web and writes a "
            "short briefing about it"
        ),
        expect=frozenset({"web"}),
        forbid=frozenset({"image", "app_build"}),
        note="needs current external facts → web search",
    ),
    Order(
        id="logo-image",
        request="an agent that turns a short brand brief into a generated logo image",
        expect=frozenset({"image"}),
        forbid=frozenset({"schedule"}),
        note="deliverable includes a generated image",
    ),
    Order(
        id="weekly-scheduled",
        request=(
            "an agent that every Monday morning gathers the week's crypto prices and posts a summary — it "
            "should schedule itself to run on that recurring clock"
        ),
        expect=frozenset({"schedule"}),
        note="explicit recurring clock → node schedule",
    ),
    Order(
        id="aimeat-app",
        request="an agent that builds a small AIMEAT web app showing the owner's latest memory notes",
        expect=frozenset({"app_build"}),
        note="deliverable is a working AIMEAT app",
    ),
    Order(
        id="pure-reasoning",
        request="an agent that rewrites a block of text I give it to be clearer and more concise",
        expect=frozenset(),
        forbid=frozenset({"web", "image", "schedule", "app_build", "memory"}),
        min_agents=1,
        note="no external tools needed — pure LLM transform of ctx.prompt",
    ),
    Order(
        id="assistant-memory",
        request=(
            "a personal assistant agent that remembers my preferences and the things I told it in earlier "
            "conversations, and uses that memory to help me better on later runs"
        ),
        expect=frozenset(),
        forbid=frozenset({"image", "app_build"}),
        expect_memory=True,
        note="must remember across runs -> persistent CrewAI memory (CrewSpec memory=True)",
    ),
]


@dataclass
class Grade:
    order_id: str
    built: bool
    wired: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # expected but not wired
    forbidden_used: list[str] = field(default_factory=list)  # forbidden but wired
    agents: int = 0
    tasks: int = 0
    prompt_used: bool = False
    has_identity: bool = False  # emitted _TAGS/_CAPABILITIES (real identity) — informational
    has_offer: bool = False  # emitted an inline _OFFER (advertises value) — informational
    has_memory: bool = False  # emitted CrewSpec memory=True (persistent CrewAI memory) — informational
    memory_missing: bool = False  # the order expected persistent memory but the crew did not enable it
    detail: str = ""

    @property
    def caps_ok(self) -> bool:
        return not self.missing and not self.forbidden_used

    @property
    def structure_ok(self) -> bool:
        return self.agents >= 1 and self.tasks >= 1 and self.prompt_used

    @property
    def passed(self) -> bool:
        return self.built and self.caps_ok and self.structure_ok and not self.memory_missing


def grade(order: Order, path: Path | None) -> Grade:
    """Grade a generated crew file against its order. Pure: validates + inspects the file, no LLM."""
    if path is None or not Path(path).exists():
        return Grade(order.id, built=False, detail="no crew file was produced")
    src = Path(path).read_text(encoding="utf-8")
    ok, detail = forge.validate_crew_file(Path(path))
    wired = forge_catalog.capabilities_in_source(src)
    m = _COUNTS_RE.search(detail or "")
    agents, tasks = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    # Persistent CrewAI memory is a CrewSpec toggle (memory=True), not an in-crew tool, so
    # capabilities_in_source can't see it — detect it directly from the rendered CrewSpec.
    has_memory = bool(re.search(r"\bmemory\s*=\s*True", src))
    return Grade(
        order_id=order.id,
        built=ok,
        wired=wired,
        missing=sorted(order.expect - set(wired)),
        forbidden_used=sorted(order.forbid & set(wired)),
        agents=agents,
        tasks=tasks,
        prompt_used=("ctx.prompt" in src),
        has_identity=("_TAGS" in src),
        has_offer=("_OFFER" in src),
        has_memory=has_memory,
        memory_missing=(order.expect_memory and not has_memory),
        detail=detail,
    )


# --------------------------------------------------------------------------- #
# Live run (needs an LLM + network) — used by scripts/eval_crew_forge.py
# --------------------------------------------------------------------------- #
def run_order(order: Order, build_domain, llm, root: Path) -> Path | None:
    """Run crew-forge's Architect+Builder for one order into `root`, DRY (no register/launch).

    `build_domain` is crew_forge_crew.build_domain. The register/launch primitives are neutralized for
    the duration so the Builder writes + validates the crew file but never contacts the node.
    """
    from crewai import Crew, Process

    from crewaimeat.aimeat_crew import BuildContext

    root = Path(root)
    (root / "crews").mkdir(parents=True, exist_ok=True)
    ctx = BuildContext(task={"id": f"eval-{order.id}"}, prompt=order.request, llm=llm, today="Today is 2026-07-01.")
    agents, tasks = build_domain(ctx)

    # Sandbox: redirect all writes to `root` and turn register/launch into no-ops so nothing touches
    # aimeat.io or spawns a process. The forge @tools look these names up on the module at call time.
    saved = {
        "_project_root": forge._project_root,
        "register_agent": forge.register_agent,
        "launch_crew": forge.launch_crew,
        "is_crew_running": forge.is_crew_running,
    }
    forge._project_root = lambda: root
    forge.register_agent = lambda *a, **k: (True, "dry-run: not registered")
    forge.launch_crew = lambda *a, **k: (None, "dry-run: not launched")
    forge.is_crew_running = lambda *a, **k: False
    try:
        Crew(agents=agents, tasks=tasks, process=Process.sequential, verbose=False).kickoff()
    finally:
        for name, fn in saved.items():
            setattr(forge, name, fn)

    files = sorted((root / "crews").glob("*_crew.py"))
    return files[-1] if files else None


def run_eval(build_domain, orders: list[Order] | None = None, *, root: Path, llm=None, model: str | None = None):
    """Run every order and grade it. Returns (grades, ...). Needs an LLM; pass one or a model name."""
    orders = orders or ORDERS
    if llm is None:
        from crewaimeat.llm import get_llm

        llm = get_llm("crew-forge", model=model) if model else get_llm("crew-forge")
    grades: list[Grade] = []
    for order in orders:
        order_root = Path(root) / order.id
        try:
            path = run_order(order, build_domain, llm, order_root)
        except Exception as exc:  # noqa: BLE001 — one order failing must not abort the run
            grades.append(Grade(order.id, built=False, detail=f"run error: {type(exc).__name__}: {exc}"))
            continue
        grades.append(grade(order, path))
    return grades


def format_scorecard(grades: list[Grade]) -> str:
    """A compact human-readable table + summary."""
    lines = [
        f"{'ORDER':<18} {'BUILT':<6} {'CAPS':<26} {'A/T':<7} {'PROMPT':<7} {'ID/OF/M':<10} RESULT",
        "-" * 90,
    ]
    for g in grades:
        caps = ",".join(g.wired) or "(none)"
        if g.missing:
            caps += f"  MISSING:{','.join(g.missing)}"
        if g.memory_missing:
            caps += "  MISSING:memory(crew)"
        if g.forbidden_used:
            caps += f"  FORBIDDEN:{','.join(g.forbidden_used)}"
        idof = (
            ("id" if g.has_identity else "-")
            + "/"
            + ("of" if g.has_offer else "-")
            + "/"
            + ("mem" if g.has_memory else "-")
        )
        result = "PASS" if g.passed else "FAIL"
        lines.append(
            f"{g.order_id:<18} {('yes' if g.built else 'NO'):<6} {caps[:26]:<26} "
            f"{f'{g.agents}/{g.tasks}':<7} {('yes' if g.prompt_used else 'NO'):<7} {idof:<10} {result}"
        )
        if not g.passed and g.detail:
            lines.append(f"    -> {g.detail[:200]}")
    passed = sum(1 for g in grades if g.passed)
    lines += ["-" * 90, f"{passed}/{len(grades)} orders passed  (ID/OF/M = emitted identity / offer / memory)"]
    return "\n".join(lines)
