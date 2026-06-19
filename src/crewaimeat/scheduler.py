"""Scheduler tools — let a crew create AIMEAT server-run schedules.

The NODE owns the clock: a schedule is created in AIMEAT and the server runs it on a cron clock, so it
fires even when the agent is offline. The owner sees every agent-created schedule (Profile -> Scheduler)
and can pause/cancel any of them. Three execution kinds:
  - extension : run an installed extension action (sandbox, 0 tokens) — best for fetch+store
  - ai        : server-side OpenRouter completion on the OWNER's key over owner memory keys (no agent)
  - agent_task: queue a task into an agent's queue each fire -> that agent runs it with its own tools

Backed by the node REST API (POST/GET /v1/agents/:name/schedules, PATCH/DELETE /v1/schedules/:id) using
the agent's own owner-scoped token. (The MCP aimeat_schedule_* tools exist in connector >=1.18 but only via
`connect serve`; the shell `connect call` path the crews' _aimeat_call uses reports them "Unknown
CLI-callable tool", so we go straight to REST — a proven create->list->delete round-trip.)

IMPORTANT contract note (verified against the live node, the human-facing guide drifted): an `agent_task`
schedule needs `task_template:{title,description}` — NOT flat task_title/task_description.

Usage (in a crew's build_domain):
    from crewaimeat.scheduler import make_schedule_tools
    sched_tools = make_schedule_tools(AGENT_NAME)
    agent = Agent(..., tools=[*other_tools, *sched_tools], llm=ctx.llm)
"""

from __future__ import annotations

import json

import requests
from crewai.tools import tool

from crewaimeat.generator_tool import _discover_owner, _token

SCHED_TIMEOUT = 30


def make_schedule_tools(agent_name: str, owner: str | None = None) -> list:
    """Return the scheduler crewai tools (schedule_create / list / update / delete) for this agent."""
    owner = owner or _discover_owner(agent_name)

    def _req(method: str, path: str, body: dict | None = None, as_agent: str | None = None):
        who = as_agent or agent_name
        tok, url = _token(who, owner)
        if not tok or not url:
            return None, f"no token/url for '{who}' (is it registered + approved? same owner?)"
        base = url.rstrip("/")
        try:
            r = requests.request(
                method,
                f"{base}{path}",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                json=body,
                timeout=SCHED_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001
            return None, f"request failed: {e!r}"
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {"_raw": (r.text or "")[:300]}
        if r.status_code >= 400:
            err = (data or {}).get("error") or data
            return None, f"HTTP {r.status_code}: {json.dumps(err)[:300]}"
        return data, None

    def _managed() -> list:
        data, err = _req("GET", f"/v1/agents/{agent_name}/schedules")
        if err:
            return []
        return ((data or {}).get("data") or {}).get("managed") or []

    @tool("schedule_create")
    def schedule_create(
        kind: str,
        cron: str,
        display_name: str,
        purpose: str,
        timezone: str = "Europe/Helsinki",
        target_agent: str = "",
        task_title: str = "",
        task_description: str = "",
        prompt: str = "",
        input_keys_json: str = "",
        output_key: str = "",
        model: str = "",
        extension_name: str = "",
        action_id: str = "",
    ) -> str:
        """Create a SERVER-RUN schedule on AIMEAT (the node owns the cron clock — it fires even when you
        are offline, and the owner sees + can pause/cancel it in Profile -> Scheduler).

        kind (pick the lightest that fits — AIMEAT-first):
          'extension'  — run an installed extension action (0 tokens, no agent). Set extension_name + action_id.
          'ai'         — server-side OpenRouter completion on the OWNER's key over owner memory keys (no
                         agent). Set prompt, input_keys_json (JSON array of owner memory keys read as
                         context), output_key (where the result is stored), optional model. Requires the
                         owner's OpenRouter key configured, else the run fails (shows in the owner's log).
          'agent_task' — queue a task into an agent's queue each fire (for work needing an agent's tools).
                         Set target_agent (same owner; default = yourself), task_title, task_description
                         (the instruction the target runs each fire — name the EXACT memory keys it
                         reads/writes). The target must be in task-runner mode or the task waits for the
                         owner to press Start.
        cron: 5-field, e.g. '0 6 * * *' = 06:00 daily. ALWAYS pass a timezone (IANA, e.g. 'Europe/Helsinki')
        for daily schedules (DST). display_name + purpose are shown to the owner.
        Schedules have NO 'run B after A' dependency — chain STAGES by cron times (e.g. fetch 06:00, refine
        07:00) and connect them through memory keys you name in each step. Returns the new schedule id."""
        kind = (kind or "").strip()
        body: dict = {"kind": kind, "cron": cron, "display_name": display_name, "purpose": purpose}
        if timezone:
            body["timezone"] = timezone
        post_as = agent_name  # ai/extension run server-side, created under this agent
        if kind == "agent_task":
            if not task_title:
                return "FAILED: agent_task needs task_title (+ task_description)."
            body["task_template"] = {"title": task_title, "description": task_description}
            # Cross-agent dispatch: the TARGET is the URL path. The node resolves it under the CALLER's
            # own owner, so this agent's OWN token may schedule a same-owner sibling (no token-borrow).
            # AIMEAT-confirmed + deployed 2026-06-03 (incl. createdByAgent fix → the creating agent can
            # also list/trigger/manage it). REST also accepts flat task_title/task_description now.
            post_as = target_agent or agent_name
        elif kind == "ai":
            if not prompt:
                return "FAILED: ai kind needs a prompt."
            body["prompt"] = prompt
            if input_keys_json:
                try:
                    body["input_keys"] = json.loads(input_keys_json)
                except Exception:  # noqa: BLE001
                    return 'FAILED: input_keys_json must be a JSON array of owner memory keys, e.g. ["news.today.raw"].'
            if output_key:
                body["output_key"] = output_key
            if model:
                body["model"] = model
        elif kind == "extension":
            if not extension_name or not action_id:
                return "FAILED: extension kind needs extension_name + action_id."
            body["extension_name"] = extension_name
            body["action_id"] = action_id
        else:
            return f"FAILED: unknown kind '{kind}'. Use 'extension', 'ai', or 'agent_task'."
        data, err = _req("POST", f"/v1/agents/{post_as}/schedules", body)  # caller's OWN token; path = target
        if err:
            return f"FAILED to create schedule (target '{post_as}'): {err}"
        sched = (data or {}).get("data") or {}
        sid = sched.get("id") or (sched.get("schedule") or {}).get("id")
        tgt = f", dispatches to '{post_as}'" if kind == "agent_task" else ""
        return (
            f"OK: schedule created (id={sid}, kind={kind}, cron='{cron}' {timezone}{tgt}). The owner "
            f"can see/pause/cancel it in Profile -> Scheduler."
        )

    @tool("schedule_list")
    def schedule_list() -> str:
        """List the schedules YOU created (id, kind, cron, enabled, last/next run). Call before updating
        or deleting one. Does not show extension-internal or other agents' schedules."""
        items = _managed()
        if not items:
            return "No schedules created by this agent yet."
        out = []
        for s in items:
            out.append(
                f"- id={s.get('id')} | kind={s.get('kind')} | cron='{s.get('cron')}' "
                f"{s.get('timezone', '')} | enabled={s.get('enabled')} | "
                f"name={s.get('displayName') or s.get('display_name')} | "
                f"next={s.get('nextRunAt') or s.get('next_run_at') or '?'} | runs={s.get('runCount', s.get('run_count', 0))}"
            )
        return "Your schedules:\n" + "\n".join(out)

    @tool("schedule_update")
    def schedule_update(
        schedule_id: str, enabled: str = "", cron: str = "", timezone: str = "", display_name: str = ""
    ) -> str:
        """Update one of your schedules (get its id from schedule_list). enabled='false' PAUSES it,
        'true' RESUMES it; cron/timezone/display_name change the rest. Re-arms the live cron immediately.
        Pass only the fields you want to change."""
        body: dict = {}
        if enabled:
            body["enabled"] = str(enabled).strip().lower() in ("true", "1", "yes", "on")
        if cron:
            body["cron"] = cron
        if timezone:
            body["timezone"] = timezone
        if display_name:
            body["display_name"] = display_name
        if not body:
            return "Nothing to update — pass enabled / cron / timezone / display_name."
        data, err = _req("PATCH", f"/v1/schedules/{schedule_id}", body)
        if err:
            return f"FAILED to update schedule {schedule_id}: {err}"
        return f"OK: schedule {schedule_id} updated ({', '.join(body.keys())})."

    @tool("schedule_delete")
    def schedule_delete(schedule_id: str) -> str:
        """Cancel and remove one of your schedules (get its id from schedule_list). Future fires stop;
        already-spawned task occurrences are left intact."""
        data, err = _req("DELETE", f"/v1/schedules/{schedule_id}")
        if err:
            return f"FAILED to delete schedule {schedule_id}: {err}"
        return f"OK: schedule {schedule_id} cancelled and removed. Future fires stop."

    tools = [schedule_create, schedule_list, schedule_update, schedule_delete]
    # Side-effecting / live-state — never serve a cached result across calls.
    for _t in tools:
        try:
            _t.cache_function = lambda *_a, **_k: False
        except Exception:  # noqa: BLE001
            pass
    return tools
