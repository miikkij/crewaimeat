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

# What EVERY agent this runtime runs writes with, whatever its job: the deliverable goes to memory
# (memory:write) and the scaffold pushes the agent's identity on every start (agent:write). Measured
# 2026-10-02 by aimeat-protocol on a sandbox: an agent proposed with memory:read alone read its data and
# then failed -- the node refused aimeat_memory_write and aimeat_agent_tags_set, the task ended `failed`
# and the spawner logged exit 3; the same definition with these two added finished `done` in 64 s. So a
# proposal carries these beside what the job needs, and `crew.menu` states them as `required_scopes` so a
# proposer reads them from the runtime instead of carrying a copy that falls behind.
RUNTIME_WRITE_SCOPES: tuple[str, ...] = ("memory:write", "agent:write")

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
