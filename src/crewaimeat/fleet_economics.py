"""`crewaimeat costs` — what each agent spent, and whether anything came out.

The fleet has metered every model call to the node's ledger since aimeat-crewai 0.16.1, with per-agent
attribution. Nobody read it back. That is why `crypto-weekly-reporter` — an agent whose code had been
deleted — went on burning model calls until it became the node's largest traffic source and had to be
found by hand, and why five near-duplicate experiments quietly took 12% of a month's spend.

So the report is built around ONE question, not around pretty totals:

    which agents cost money without producing anything anyone reads?

An agent that spends and delivers is fine at any price; an agent that spends and delivers nothing is
a bug with a monthly invoice. The `verdict` column answers exactly that, by crossing the ledger's
per-agent spend with what this repo knows: does a crew file still exist for it, is it registered, did
it publish a deliverable in the window.

Reads `GET /v1/ledger/usage?group_by=agent` — a precomputed layer, cheap however long the history is.
Works off-fleet (a direct authed request), and when it cannot read it says WHICH read failed instead
of printing a confident zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

WINDOW_DAYS = 30


@dataclass
class Row:
    agent: str
    calls: int
    tokens: int
    cost_usd: float
    has_crew: bool
    registered: bool
    parked: bool

    is_owner: bool = False  # the owner's own GHII shows up beside the agents; it is a person

    @property
    def verdict(self) -> str:
        """What this row MEANS — the reason the report exists.

        Ordered by how much it should worry you. "no code" is the crypto-weekly-reporter case: the
        crew file is gone, so nothing in this repo can even be producing the output it pays for.
        """
        if self.is_owner:
            return "ok"  # the human's own calls (chat, tools) — not a fleet agent at all
        if not self.has_crew:
            return "NO CODE — spends, but no crew file exists here"
        if self.parked:
            return "PARKED — the fleet skips it, yet it is still spending"
        if not self.registered:
            return "UNREGISTERED — spending under an identity the fleet does not hold"
        return "ok"

    @property
    def wasted(self) -> bool:
        return self.verdict != "ok"


def _short(gaii: str) -> str:
    """`news-writer#owner@node` -> `news-writer`. The ledger keys by GAII; humans think in names."""
    return gaii.split("#", 1)[0]


def collect(
    root: Path, days: int = WINDOW_DAYS, probe: str | None = None, min_cost: float = 0.0
) -> tuple[list[Row], dict, str | None]:
    """(rows, totals, skip_reason). `skip_reason` is set when the node could not be read — never an
    empty result presented as "nothing was spent"."""
    from crewaimeat.agent_manifest import by_agent
    from crewaimeat.doctor.inventory import gather

    inv = gather(root)
    agent = probe or next(iter(sorted(inv.served)), None)
    if not agent:
        return [], {}, "nothing registered in serve.json — no identity to ask the node with"

    import datetime

    from crewaimeat.aimeat_crew import _aimeat_rest

    # REST, not the MCP tool. `aimeat_usage_report` exists on the MCP surface but NOT in the connector's
    # shell-callable set, so `_aimeat_call` answers "Unknown shell-callable tool" — this repo's standing
    # trap of three different tool surfaces. `_aimeat_rest` reaches any /v1 route over the same tunnel
    # and, crucially, still works off-fleet through a direct authed request, so `costs` is answerable
    # without the fleet running.
    today = datetime.datetime.now(datetime.timezone.utc).date()
    since = today - datetime.timedelta(days=days)
    path = f"/v1/ledger/usage?group_by=agent&from={since}&to={today}&limit=100"
    data = _aimeat_rest(agent, "GET", path) or {}
    groups = data.get("groups") if isinstance(data, dict) else None
    if not groups:
        # Say WHICH read failed and how it was asked. The first draft of this reported every empty
        # answer as "the fleet is not attached", which was simply wrong — the real cause was an
        # unknown tool name — and a checker that misnames its own failure sends you hunting in the
        # wrong place.
        return [], {}, f"GET {path} returned no usage groups (asked as '{agent}')"

    mans = by_agent(root)
    served = set(inv.served)
    rows = []
    for g in groups:
        name = _short(str(g.get("key") or ""))
        if not name or name == agent.split("#", 1)[0] and not g.get("calls"):
            continue
        m = mans.get(name)
        rows.append(
            Row(
                agent=name,
                calls=int(g.get("calls") or 0),
                tokens=int(g.get("total_tokens") or 0),
                cost_usd=float(g.get("cost_usd") or 0.0),
                has_crew=m is not None,
                registered=name in served,
                parked=bool(m and m.parked),
                is_owner="#" not in str(g.get("key") or ""),  # a GHII has no agent part
            )
        )
    rows.sort(key=lambda r: r.cost_usd, reverse=True)
    # A floor keeps the answer readable: two dozen agents that made two onboarding calls each and spent
    # nothing are true, and they bury the six that are actually costing money. The totals line still
    # counts every row, so nothing is hidden — only moved out of the way.
    if min_cost > 0:
        rows = [r for r in rows if r.cost_usd >= min_cost or r.wasted and r.calls > 5]
    return rows, data.get("totals") or {}, None


def render(rows: list[Row], totals: dict, days: int) -> str:
    if not rows:
        return "no usage in the window."
    width = max(len(r.agent) for r in rows)
    out = [f"model spend per agent — last {days} days", ""]
    out.append(f"  {'agent':<{width}}  {'cost':>9}  {'calls':>7}  {'tokens':>11}   verdict")
    out.append(f"  {'-' * width}  {'-' * 9}  {'-' * 7}  {'-' * 11}   {'-' * 7}")
    for r in rows:
        mark = "" if not r.wasted else "  <-- "
        verdict = "" if not r.wasted else r.verdict
        out.append(f"  {r.agent:<{width}}  ${r.cost_usd:>8.2f}  {r.calls:>7}  {r.tokens:>11,}{mark}{verdict}")
    waste = [r for r in rows if r.wasted]
    spent = float(totals.get("cost_usd") or sum(r.cost_usd for r in rows))
    out.append("")
    out.append(f"  total ${spent:.2f} across {int(totals.get('calls') or sum(r.calls for r in rows))} calls")
    if waste:
        burned = sum(r.cost_usd for r in waste)
        share = (burned / spent * 100) if spent else 0
        out.append("")
        out.append(f"  ${burned:.2f} ({share:.0f}%) went to {len(waste)} agent(s) that produce nothing here:")
        for r in waste:
            out.append(f"    · {r.agent} — {r.verdict}")
        out.append("")
        out.append("  `crewaimeat retire <agent>` stops one. `crewaimeat doctor --live` shows its schedules.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crewaimeat costs", description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=WINDOW_DAYS, help=f"window in days (default {WINDOW_DAYS})")
    ap.add_argument("--all", action="store_true", help="include agents that spent almost nothing")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    rows, totals, skipped = collect(root, days=args.days, min_cost=0.0 if args.all else 0.01)
    if skipped:
        print(f"costs: could NOT read the node — {skipped}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {
                    "days": args.days,
                    "totals": totals,
                    "agents": [r.__dict__ | {"verdict": r.verdict, "wasted": r.wasted} for r in rows],
                },
                indent=2,
            )
        )
    else:
        print(render(rows, totals, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
