# Agent-Bundled App demo — Notes Summarizer

The Slice-1 acceptance artifact for **Agent-Bundled Apps** (living spec: Internal workspace
`ws-mq5vvdgsjwp`, plan doc `doc-76ab674`). `notes_summarizer_app.json` is a complete app record whose
manifest embeds a declarative crew-def under `cortex.agents` — deploying it makes the OWNER'S OWN fleet
instantiate the agent. Single-tenant, owner-scoped, no arbitrary code: the crew-def is DATA the fleet
interprets exec-free (`crewaimeat.crew_def`), gated by `crewaimeat.app_deploy`.

## The pieces

- `cortex.agents[0]` — the `notes-summarizer` crew-def (one pure-reasoning agent, no tools, injects
  `{{ctx.prompt}}`). It passes `crewaimeat.app_deploy.validate_app_crew_def` — the deterministic tests
  in `tests/test_app_deploy.py` assert exactly this file validates and maps to (agents, tasks).
- Deployed fleet name (contract §3.4): `notes-summarizer-agent-notes-demo`
  (= `<agent_name>-<slug(app_id)>`).
- Liveness key: `agents.notes-summarizer-agent-notes-demo.deploy` (visibility owner) —
  `{app_id, agent_name, deployed_agent_name, status: live|undeployed, ts}`.

## Manual integration run (own fleet)

1. **Publish the app** with the record in `notes_summarizer_app.json` (`aimeat_app_publish`; the node
   must accept/keep the `cortex.agents` field — aimeat-protocol phase 5).
2. **Deploy** — the node's deploy flow (phase 6), or by hand: create a task on your `crew-forge` with
   scope `[{"name":"kind","value":"deploy-app-agent"}, {"name":"app_id","value":"agent-notes-demo"},
   {"name":"agent_name","value":"notes-summarizer"}, {"name":"owner","value":"<your AIMEAT owner>"}]`.
3. **Assert live**: the deploy key reads `status:"live"`, and the agent profile for
   `notes-summarizer-agent-notes-demo` exists. Approve the one-time device flow if this is the first
   registration. Queue it a notes-summarization task to see it work.
4. **Redeploy** the same task — expect a `no-op (idempotent)` report, nothing double-launched.
5. **Undeploy**: same scope with `kind:"undeploy-app-agent"` — the daemon stops, the materialized
   `crews/…_crew.py` + `crew_defs/….json` are removed, and the key flips to `status:"undeployed"`.

Fleet-side guards you should NOT be able to get around: a foreign `owner` in scope or on the app
record is a hard reject; a tool id outside `forge_catalog`'s vetted set is a reject; a skill not
already installed under the fleet's `skills/` is a reject; every rejection is loud in the task result
(`REJECTED: …`) and the daemon log.
