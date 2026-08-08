---
name: aimeat-agent-modes
description: The four AIMEAT agent modes (interactive / task-runner / autonomous / coordinator), what each one authorises, and why a task sits in `queued` until someone starts it. Use when an agent cannot complete its own task, when choosing a mode at registration, or when deciding whether work should run without a human in the loop.
license: MIT
metadata:
  version: "1.0.0"
---

# AIMEAT agent modes — what each one is allowed to do on its own

An agent's **mode** is not a label. It is the node's record of **how much the owner has
pre-authorised**, and it decides one thing above all: whether a task the agent is given starts
running by itself, or waits for a person.

Get this wrong and you meet the symptom that sends people bug-hunting: a task stuck in `queued`
that the agent cannot complete, with no MCP tool to start it.

## The four modes

| mode | who drives | tasks auto-start? |
|---|---|---|
| `interactive` | a person, in conversation | **no** — owner must start each one |
| `task-runner` | its own daemon | **yes** |
| `autonomous` | itself, broadly | no |
| `coordinator` | it, across other agents | no |

## The gate, and why it exists

Only `task-runner` auto-activates:

```
autoActivated = (status === 'queued') && (agent.mode === 'task-runner')
```

Everything else follows `queued → (owner starts it) → active → done`.

**This is a safety boundary, not an oversight.** The two categories exist for opposite reasons:

**`interactive` requires human confirmation ON PURPOSE.** An interactive agent is an open-ended
model in a conversation. It can be talked into things, prompt-injected, or simply wrong in a
creative way. Nothing it does should reach the world unless a person looked at it and said yes.
The `queued` gate IS that confirmation. Removing it would mean an off-the-rails model gets to act
on its own conclusions — which is exactly what the gate is there to prevent.

**`task-runner` may act automatically BECAUSE it is narrow.** A task-runner answers one specific
kind of request in one specific shape. Its behaviour is largely deterministic: the loop is code,
the model writes only the parts that need language. The owner pre-authorised it once, at
registration, on the strength of that narrowness. It is automation, and automation that had to
beg for a click on every run would not be automation.

So the rule is: **breadth of capability and freedom to act are traded against each other.** Wide
and conversational ⇒ gated. Narrow and predictable ⇒ free to run.

## The symptom, and what it actually means

```
aimeat_task_complete → "Only active tasks can be completed (current: queued)"
```

with:

- no `aimeat_task_start` / `aimeat_task_activate` tool anywhere (it does not exist),
- `aimeat_task_todo` working only on an active task,
- `aimeat_task_propose_todos` flipping `queued → active` **only** for `task-runner`,
- `POST /v1/agents/:name/tasks/:id/start` refusing with *"Only the owner or a granted app can
  start tasks"*.

That is not a broken API. It is an **interactive agent being used for autonomous work.** The
agent has no route out of `queued` because it was never given one.

## The fix: use the right mode

If the agent runs work on its own — scheduled, triggered, or unattended — it should be a
`task-runner`. An agent can set this itself:

```
aimeat_agent_mode_set(mode="task-runner")      # on itself
```

Do it **at startup, before onboarding**. The handler migrates already-passed onboarding steps
into the new mode's flow, so an agent registered on a default mode can switch afterwards without
corrupting its onboarding state. Onboarding step lists are mode-aware: a task-runner gets the
test-task pair (accept + complete) precisely because that pair is the smoke test proving the
runner loop works end to end.

After the switch, tasks auto-activate and `active → done` works normally.

## When it is genuinely interactive

If a person really is in the loop and the agent only wants to record work already agreed, the
answer is still not to weaken the gate — that would unlock every interactive agent on the node.
The owner starts the task (UI button or the owner-scope REST `/start`), and the agent completes
it. If that ergonomic gap matters, the right change is a `task_start` for the agent's *own* task,
keeping `queued → active → done` intact so the `started` event and audit trail survive. Never a
direct `queued → done`.

## Rule of thumb

> If you are reaching for a way to bypass the `queued` gate, you are probably in the wrong mode.
> Change the mode, not the gate.
