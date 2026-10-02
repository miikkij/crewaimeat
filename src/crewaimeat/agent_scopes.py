"""The permissions an agent asks for when it is approved: one list, for every place that registers one.

WHY ASK AT ALL. A connector that names no scopes gets the node's four defaults, and the owner has to know
to tick more on the consent page. Nobody did: on a sold seat the crew's identity push
(`PATCH /v1/agents/concierge/tags`) needed agent:write, the agent held the four defaults, and the run
exited 0 with every write refused (2026-09-29). Since aimeat 3.21.0, `aimeat connect --scopes a,b,c`
puts the scopes in the device-authorization request, the consent page shows them, and the node keeps
what was asked beside what was granted. (That push needs no word of its own since aimeat-protocol
bcd4027ed, and is skipped when the node already holds the tags; what remains to ask for is what a
crew's TOOLS need, and the defaults.)

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

# What a run of EVERY agent needs beyond the defaults: NOTHING, measured against the routes a run
# calls (aimeat-protocol main, 2026-10-02):
#   the deliverable, the README, `crews.runtime.<name>`     aimeat_memory_write        memory:write (a default)
#   its own tags, on every start                            aimeat_agent_tags_set      no word for its OWN record
#                                                           since bcd4027ed (requireScopeUnlessSelf); an older
#                                                           node wants agent:write, and the push is SKIPPED
#                                                           when the node already holds the tags
#                                                           (aimeat_crew._set_tags_if_changed)
#   its own capabilities, runtime, onboarding steps         ..._capabilities_report,   no word (SCOPE_EXEMPT_TOOLS)
#                                                           ..._runtime_report
#   its own tasks: list, plan, close, fail                  aimeat_task_*              no word (isOwnTask)
# The four words that were here, and why each left:
#   agent:write     the tags push -- no longer needed for the agent's own record, and it is also the word
#                   that lets an agent approve a new agent by itself (routes/agents/device-auth.ts), which
#                   the basic agents must never hold: the concierge reads messages from strangers.
#   task:write      creating an `agent_task` schedule (schedule-gate.ts) and starting a sibling's task --
#                   a crew that wires `schedule` asks for it through forge_catalog; closing its OWN task
#                   never needed it.
#   workflow:read   GET /v1/schedules -- the same `schedule` capability, the retire probe and agency 2.0.
#   wallet:read     GET /v1/ledger/usage -- `crewaimeat costs`, pulse and agency 2.0's costs view, all
#                   read by a probe or a product, not by a crew's run.
# A crew's EXTRA needs come from the tools it wired (forge_catalog.required_scopes); agency 2.0 names what
# its own features call as every agent it manages (agency2.connect.REQUIRED_SCOPES).
REQUIRED_SCOPES: tuple[str, ...] = ()

# What EVERY agent this runtime runs writes with, whatever its job: the deliverable goes to memory.
# Measured 2026-10-02 by aimeat-protocol on a sandbox: an agent proposed with memory:read alone read its
# data and then failed -- the node refused aimeat_memory_write, the task ended `failed` and the spawner
# logged exit 3. So a proposal carries this beside what the job needs, and `crew.menu` states it as
# `required_scopes` so a proposer reads it from the runtime instead of carrying a copy that falls behind
# (the node keeps its own copy, data/crew-runtime-scopes.ts, only as the fallback for an agent with no
# runtime yet). agent:write is NOT in it and must not be: see REQUIRED_SCOPES above.
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
