"""The permissions an agent asks for when it is approved: one list, for every place that registers one.

WHY ASK AT ALL. A connector that names no scopes gets the node's four defaults, and the owner has to know
to tick more on the consent page. Nobody did: on a sold seat the crew's identity push
(`PATCH /v1/agents/concierge/tags`) needed agent:write, the agent held the four defaults, and the run
exited 0 with every write refused (2026-09-29). Since aimeat 3.21.0, `aimeat connect --scopes a,b,c`
puts the scopes in the device-authorization request, the consent page shows them, and the node keeps
what was asked beside what was granted.

THE DEFAULTS ARE IN THE LIST ON PURPOSE. For a NEW agent the node grants what was requested INSTEAD of
its default (routes/agents/device-auth.ts v1.9.0), so a request that named only what the scaffold needs
beyond the defaults would approve an agent with no memory access -- and memory is where every crew puts
its deliverable. `NODE_DEFAULT_SCOPES` is the node's stock default (config.ts, AIMEAT_DEFAULT_AGENT_SCOPES);
an operator can change theirs, and a crew still needs these four whatever the node's default says.

NOT ON A RE-APPROVAL. For an agent that already exists, requested scopes REPLACE what it holds (only the
scopes no wildcard carries survive). Asking again would cut an agent the owner gave `*` down to this list
without anyone deciding that. A reconnect that names nothing keeps what the owner granted, which is the
node's own rule, so a caller asks only when it is registering an agent for the first time -- or when the
point of reconnecting is to change the scopes (agency 2.0's reconnect).
"""

from __future__ import annotations

from collections.abc import Iterable

# The node's stock default for an agent that names nothing (aimeat config.ts, defaultAgentScopes).
NODE_DEFAULT_SCOPES: tuple[str, ...] = ("memory:read", "memory:write", "memory:delete", "catalogue:read")

# What the scaffold needs beyond the defaults, measured against the routes that answer it:
#   agent:write     the identity push on every start (tags) — without it tags_set answers SCOPE_DENIED
#   task:write      closing its own tasks, and creating its own `agent_task` schedule (schedule-gate.ts)
#   workflow:read   listing schedules (GET /v1/schedules)
#   wallet:read     reading what the agents spent (GET /v1/ledger/usage, routes/ledger.ts)
REQUIRED_SCOPES: tuple[str, ...] = ("agent:write", "task:write", "workflow:read", "wallet:read")

# What EVERY agent this runtime runs writes with, whatever its job: the deliverable goes to memory.
# Measured 2026-10-02 by aimeat-protocol on a sandbox: an agent proposed with memory:read alone read its
# data and then failed -- the node refused aimeat_memory_write, the task ended `failed` and the spawner
# logged exit 3. So a proposal carries this beside what the job needs, and `crew.menu` states it as
# `required_scopes` so a proposer reads it from the runtime instead of carrying a copy that falls behind.
# agent:write WAS here, for the identity push (tags) and the runtime report on every start. Since
# aimeat-protocol bcd4027ed and 6e999056d an agent sets its OWN tags and reports its OWN runtime with no
# permission word, and agent:write is also the word that lets an agent approve a new agent by itself --
# so an agent that does not need it must not be asked for it (the node's own list, data/crew-runtime-scopes.ts,
# is memory:write alone). REQUIRED_SCOPES above keeps it for registering against an older node.
RUNTIME_WRITE_SCOPES: tuple[str, ...] = ("memory:write",)

# THE NODE ROAD (llm_choice `{kind:'node'}`): the crew's model calls go to the node's /v1/llm, which
# requires `ai:use` (aimeat routes/llm-proxy.ts). Asked for whenever the owner routes the agent there.
NODE_ROAD_SCOPES: tuple[str, ...] = ("ai:use",)


def runtime_scopes(*, node_road: bool) -> list[str]:
    """What every run writes with, plus what the node road needs when the agent takes it."""
    return [*RUNTIME_WRITE_SCOPES, *(NODE_ROAD_SCOPES if node_road else ())]


# The connector release whose `connect` takes `--scopes`. An older CLI refuses an undeclared option and
# the whole registration fails, so a caller that cannot vouch for its connector checks this first.
SCOPES_FLAG_SINCE = "3.21.0"


def requested_scopes(extra: Iterable[str] = ()) -> list[str]:
    """Defaults, then what the scaffold needs, then `extra` (a crew's own tools), each once, in order."""
    out: list[str] = []
    for s in (*NODE_DEFAULT_SCOPES, *REQUIRED_SCOPES, *extra):
        s = str(s).strip()
        if s and s not in out:
            out.append(s)
    return out


def scopes_args(extra: Iterable[str] = ()) -> list[str]:
    """The two argv elements for `aimeat connect`: ["--scopes", "a,b,c"]."""
    return ["--scopes", ",".join(requested_scopes(extra))]
