"""Try a definition once, locally, before it is anything — in its own process.

    python -m crewaimeat.agency2.trial <job-dir>      # reads job.json, writes result.json

The cockpit never imports crewai: a trial is a real kickoff (the same interpreter the runtime uses,
`crew_def.build_domain_from_json`, like `crewaimeat try`), and it runs here, in a child. Nothing is
registered, published or written to a node. `as_agent` borrows a connected agent's identity for node
tools (memory, …); a brand-new agent has none yet, so only tools that need no node (web) can act.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from crewaimeat.agency2 import engine, paths

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _jobs_dir() -> Path:
    d = paths.state_dir() / "trials"
    d.mkdir(parents=True, exist_ok=True)
    return d


def start(doc: dict, prompt: str, *, as_agent: str | None = None) -> str:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("trial.start spawns a crew run — never under pytest")
    tid = uuid.uuid4().hex[:12]
    d = _jobs_dir() / tid
    d.mkdir()
    paths.write_json(d / "job.json", {"doc": doc, "prompt": prompt, "as_agent": as_agent})
    with _LOCK:
        _JOBS[tid] = {"status": "running", "started": time.time()}

    def _run() -> None:
        with open(d / "trial.log", "wb") as log:
            rc = subprocess.run(
                [engine.python_exe(), "-m", "crewaimeat.agency2.trial", str(d)],
                cwd=str(paths.data_dir()),
                env=engine.child_env(),
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).returncode
        res = paths.read_json(d / "result.json", None) or {
            "ok": False,
            "error": f"the trial process exited with {rc} and wrote no result",
            "log": _tail(d / "trial.log"),
        }
        with _LOCK:
            _JOBS[tid] = {**res, "status": "done", "seconds": round(time.time() - _JOBS[tid]["started"], 1)}

    threading.Thread(target=_run, name=f"trial-{tid}", daemon=True).start()
    return tid


def get(tid: str) -> dict | None:
    with _LOCK:
        j = _JOBS.get(tid)
        return dict(j) if j else None


def _tail(p: Path, n: int = 4000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


def run_job(job_dir: Path) -> dict:
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    doc, prompt, as_agent = job["doc"], job["prompt"], job.get("as_agent")
    from crewaimeat.crew_def import build_domain_from_json, validate_crew_doc

    errors = validate_crew_doc(doc)
    if errors:
        return {"ok": False, "error": "the definition is not valid", "errors": errors}
    from crewai import Crew, Process

    from crewaimeat.aimeat_crew import BuildContext, _now_context
    from crewaimeat.llm import get_llm, resolved_model

    identity = as_agent or str(doc.get("agent_name") or "")
    llm = get_llm(
        for_tool_use=any((a or {}).get("tools") for a in doc.get("agents") or []),
        temperature=doc.get("temperature"),
        agent_name=identity,
    )
    ctx = BuildContext(
        task={"id": "try", "title": "local try", "description": prompt},
        prompt=prompt,
        llm=llm,
        today=_now_context(),
        directives="",
    )
    agents, tasks = build_domain_from_json(dict(doc, agent_name=identity), ctx)
    crew = Crew(
        agents=agents,
        tasks=tasks,
        process=Process.hierarchical if doc.get("process") == "hierarchical" else Process.sequential,
    )
    result = crew.kickoff()
    return {"ok": True, "output": str(result), "model": resolved_model(llm)}


def main() -> None:
    job_dir = Path(sys.argv[1])
    engine.openrouter_only()
    try:
        res = run_job(job_dir)
    except Exception as exc:  # noqa: BLE001 — the real cause, for the person to read
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    paths.write_json(job_dir / "result.json", res)


if __name__ == "__main__":
    main()
