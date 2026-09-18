"""`python -m crewaimeat.agency2.spawn` — run this machine's agents only while they have work.

One small process (the scaffold's `Spawner`, which never imports crewai: ~25 MB) parks on every
local agent's wake. When the node pushes a task — a person's, or a schedule's — it starts a worker
(`crewaimeat.run_once <agent>`), and the worker's ~200 MB is returned the moment the task is done.
Before this each agent was a resident process of its own all day.

THE ROSTER IS THIS APP'S, NOT THE NODE'S. The fleet's spawner serves every agent the node marks
`run_mode = spawn` for the owners in its home. Here that would be wrong twice: a person's other
machine (or a hosted fleet) may run spawn agents of the same owner, and this app would start running
them too. So `roster_fn` reads the app's own list — the agents connected HERE and not stopped — and the
node's `run_mode` is left alone, so no other runtime is told to pick these agents up either.
"""

from __future__ import annotations


def roster() -> list[str]:
    from crewaimeat.agency2 import store

    return sorted(a["name"] for a in store.agents() if a.get("connected") and a.get("autostart", True))


def main() -> int:
    from crewaimeat import spawner
    from crewaimeat.agency2 import paths

    lock = spawner._acquire_singleton()  # one per connector home, released by the OS if we die
    if lock is None:
        print("[agency2-spawn] another spawner already serves this home — exiting", flush=True)
        return 0
    s = spawner.Spawner(agents=roster(), root=paths.data_dir(), roster_fn=roster)
    return s.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
