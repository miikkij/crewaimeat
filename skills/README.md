# skills/ — SKILL.md expertise packs for crews

One skill = one directory `skills/<skill-name>/` with a required `SKILL.md` (YAML frontmatter +
markdown body) and optional `scripts/` `references/` `assets/`. The body is injected into an
agent's prompt on activation (crewai renders it as a `<skills>` block).

Contract (shared with the AIMEAT registry side — spec doc-sdie0se in the Internal workspace):

- `name`: 1–64 chars, lowercase alphanumeric + hyphens, MUST equal the directory name.
- `description`: 1–1024 chars — "what it does + when to use" (the discovery index key).
- Optional: `license`, `compatibility`, `metadata`. `allowed-tools` is metadata only (it does
  NOT provision tools).
- Keep the body focused (< ~50k chars) — large injections dilute agent attention.

Using a skill in a crew:

```python
CrewSpec(agent_name="joker", build_domain=build_domain, skills=["comedy-set-craft"])

def build_domain(ctx):
    comic = Agent(..., llm=ctx.llm, skills=ctx.skills)  # same idiom as ctx.llm
```

Declarative JSON crew-defs take a top-level `"skills": ["name", ...]` (applies to every agent).
Skills are loaded + validated at daemon start by `crewaimeat.skills.load_skills` — a missing or
malformed skill fails the start loudly. Live proof: `crews/joker_crew.py` (comedy-set-craft).

**Registry skills (AIMEAT node):** on top of the repo-local skills above, every crew ALSO fetches
the skills the owner LINKED to its agent in the AIMEAT skills registry (profile UI or
`aimeat_skill_link`) — fresh per crew build, via `crewaimeat.skills_registry`. Merge is a union by
name and a repo-local skill wins a collision; registry unreachable → the task runs on local skills
with a loud stderr note. Opt out per crew with `CrewSpec(registry_skills=False)`. Publish a repo
skill to the registry with `POST /v1/skills {skill_md, scope:"user"}` (agent token works) so the
owner can link it to any agent without a code change.

**Workspace skills (2c, opt-in):** `CrewSpec(workspace_skills=True)` additionally attaches the
skills published in the workspaces the crew operates in (derived from its `record_spaces`; or pass
an explicit `[{"organism_id": ..., "ws": ...}]`). Precedence on a name collision:
repo-local > owner-linked > workspace-auto. DEFAULT OFF — a workspace is a shared surface, so any
member's published skill would ride into the crew's prompts; opt in only where that is the intent.
