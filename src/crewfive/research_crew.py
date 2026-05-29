"""research-crew — domain-agentit + AIMEAT-liaison, onboarding-gatella + poll-loopilla.

Mallit:
- ONBOARDING-ONLY: jos Hello Integration ei ole tehty -> liaison (LLM) hoitaa sen
  yksin. Ei domain-agentteja, ei researchia.
- WORK: kun onboarding on 'completed' -> hae oikea queued-taski AIMEATista, anna
  domain-agenttien (Tutkija->Analyytikko->Kirjoittaja) tutkia VAIN se, ja
  julkaise tulos + completaa AIMEAT-taski DETERMINISTISESTI (Python, ei LLM:ää).
  Determinismi välttää ison raportin LLM-generoinnin rikki-JSON-/NoneType-kaatumiset.

Ajo:
    uv run python -m crewfive.research_crew
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time

from dotenv import load_dotenv

load_dotenv()
for _s in (sys.stdout, sys.stderr):
    _r = getattr(_s, "reconfigure", None)
    if _r:
        _r(encoding="utf-8")

from crewai import Agent, Crew, Process, Task  # noqa: E402
from aimeat_crewai import create_liaison_agent, stdio_params  # noqa: E402

from crewfive.crew import _web_tools  # noqa: E402
from crewfive.llm import get_llm  # noqa: E402

AGENT_NAME = "research-crew"


# --------------------------------------------------------------------------- #
# AIMEAT-CLI-apuri (deterministinen, ei LLM:ää). Windowsissa cmd /c.
# Kestää ison payloadin --stdin:n kautta.
# --------------------------------------------------------------------------- #
def _aimeat_call(tool: str, payload: dict) -> dict | None:
    if shutil.which("aimeat") is None:
        return None
    base = ["aimeat", "connect", "call", tool, "--agent", AGENT_NAME, "--stdin"]
    cmd = ["cmd", "/c", *base] if os.name == "nt" else base
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(payload), capture_output=True, text=True, timeout=90
        )
        return json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        print(f"[research-crew] {tool} epäonnistui: {exc}", file=sys.stderr)
        return None


def _onboarding_completed() -> bool:
    data = _aimeat_call("aimeat_onboarding_status", {})
    return bool(data) and data.get("onboarding", {}).get("status") == "completed"


def _fetch_queued_task() -> dict | None:
    """Hakee vanhimman jonossa olevan (queued) taskin. {id,title,prompt} tai None."""
    data = _aimeat_call("aimeat_task_list", {"status": "queued", "per_page": 10})
    if not data:
        return None
    tasks = data.get("tasks", [])
    if not tasks:
        return None
    t = tasks[0]
    prompt = (t.get("description") or t.get("title") or "").strip()
    return {"id": t.get("id"), "title": t.get("title"), "prompt": prompt}


def _memory_key(task: dict) -> str:
    """research.<agent>.<slug>-<lyhyt-taskid>.latest_output (oikea task-id)."""
    tid = task.get("id") or "manual"
    short = tid.split("-", 1)[0] if "-" in tid else tid[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", (task.get("prompt") or "").lower()).strip("-")[:32].strip("-")
    token = f"{slug}-{short}" if slug else short
    return f"research.{AGENT_NAME}.{token}.latest_output"


def _make_liaison(llm):
    return create_liaison_agent(
        mcp_server_params=stdio_params(agent_name=AGENT_NAME),
        agent_name=AGENT_NAME,
        llm=llm,
        verbose=True,
    )


# --------------------------------------------------------------------------- #
# ONBOARDING-ONLY (liaison/LLM hoitaa Hello Integrationin)
# --------------------------------------------------------------------------- #
def _run_onboarding_only(llm) -> None:
    print(
        "[research-crew] Hello Integration tekemättä -> ajetaan VAIN onboarding "
        "(liaison yksin, ei domain-työtä).",
        file=sys.stderr,
    )
    with _make_liaison(llm) as liaison:
        task = Task(
            description=(
                "Complete AIMEAT Hello Integration. Work carefully and in order — do NOT "
                "rush, and do NOT fire several tool calls in the same turn.\n"
                "1. Call aimeat_onboarding_status to see which steps are pending.\n"
                "2. Complete each pending onboarding step with its matching "
                "aimeat_onboarding_* tool.\n"
                "3. For the test task: call aimeat_task_propose_todos ONCE.\n"
                "4. Mark the TODOs done with aimeat_task_todo ONE AT A TIME — one call, "
                "wait for its result, then the next. Never several in the same turn.\n"
                "5. Once all TODOs are done, you MUST call aimeat_task_complete with the "
                "test task's id to complete it. This single call makes 'complete_test_task' "
                "pass — the task is NOT finished until you call it. Do NOT re-mark done TODOs.\n"
                "6. Call aimeat_onboarding_status once more and report. Do NOT do domain work."
            ),
            expected_output="All 7 onboarding steps passed; test task completed via aimeat_task_complete.",
            agent=liaison,
        )
        Crew(
            agents=[liaison], tasks=[task], process=Process.sequential, verbose=True, cache=False
        ).kickoff()
        print("\n=== research-crew: ONBOARDING-ONLY valmis ===")


# --------------------------------------------------------------------------- #
# DETERMINISTINEN julkaisu + taskin completaus (EI LLM:ää)
# --------------------------------------------------------------------------- #
def _publish_and_complete(tid: str, key: str, report: str) -> None:
    # 1) Julkaise raportti muistiin (iso arvo ok --stdin:llä)
    r = _aimeat_call("aimeat_memory_write", {"key": key, "value": report, "visibility": "owner"})
    ok1 = bool(r) and not (isinstance(r, dict) and r.get("error"))
    print(f"[research-crew] memory_write {key}: {'ok' if ok1 else r}", file=sys.stderr)

    # 2) Aktivoi taski (propose todos) + merkitse todot doneksi
    r2 = _aimeat_call(
        "aimeat_task_propose_todos",
        {
            "task_id": tid,
            "todos": [
                {
                    "title": "Tutkimus tuotettu ja julkaistu",
                    "description": f"Domain-kruu tuotti raportin; tulos muistissa avaimella {key}.",
                }
            ],
        },
    )
    todos = ((r2 or {}).get("task") or {}).get("todos") or []
    for td in todos:
        if td.get("id"):
            _aimeat_call(
                "aimeat_task_todo", {"task_id": tid, "todo_id": td["id"], "status": "done"}
            )

    # 3) Completaa taski (lyhyt summary + viittaus muistiavaimeen)
    r3 = _aimeat_call(
        "aimeat_task_complete",
        {"task_id": tid, "message": f"Tutkimus valmis. Tulos julkaistu muistiin avaimella {key}."},
    )
    ok3 = bool(r3) and not (isinstance(r3, dict) and r3.get("error"))
    print(f"[research-crew] task_complete {tid}: {'ok' if ok3 else r3}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# WORK: domain-agentit tutkivat, Python julkaisee deterministisesti
# --------------------------------------------------------------------------- #
def _run_work(llm, task: dict) -> None:
    memory_key = _memory_key(task)
    tid = task["id"]
    print(
        f"[research-crew] Käsitellään AIMEAT-taski {tid}\n"
        f"[research-crew] Prompt: {task['prompt']}\n"
        f"[research-crew] Julkaisuavain: {memory_key}",
        file=sys.stderr,
    )

    researcher = Agent(
        role="Tutkija",
        goal="Kerää relevantti, ajantasainen taustatieto tehtävään: {request}",
        backstory=(
            "Olet perusteellinen tutkija, joka kokoaa faktat ja lähteet ennen analyysiä. "
            "Käytät web-hakua varmistaaksesi ajantasaisuuden."
        ),
        tools=_web_tools(),
        llm=llm,
        verbose=True,
    )
    analyst = Agent(
        role="Analyytikko",
        goal="Analysoi tutkijan löydökset ja tunnista keskeiset johtopäätökset",
        backstory="Olet terävä analyytikko, joka erottaa olennaisen ja tekee johtopäätökset.",
        llm=llm,
        verbose=True,
    )
    writer = Agent(
        role="Kirjoittaja",
        goal="Kirjoita selkeä, toteutuskelpoinen lopputulos analyysin pohjalta",
        backstory="Olet ammattikirjoittaja, joka tuottaa jäsenneltyä ja konkreettista tekstiä.",
        llm=llm,
        verbose=True,
    )
    research = Task(
        description="Tutki tehtävää ja kokoa keskeiset taustatiedot: {request}",
        expected_output="Lista keskeisistä löydöksistä ja faktoista lähteineen.",
        agent=researcher,
    )
    analysis = Task(
        description="Analysoi löydökset ja tunnista 3–5 keskeistä johtopäätöstä.",
        expected_output="Lyhyt analyysi keskeisine johtopäätöksineen.",
        agent=analyst,
    )
    writing = Task(
        description=(
            "Kirjoita lopputulos analyysin pohjalta tehtävään: {request}. "
            "Sisällytä otsikko, tiivistelmä, osiot ja konkreettiset suositukset."
        ),
        expected_output="Jäsennelty lopputulos: otsikko, tiivistelmä, osiot ja suositukset.",
        agent=writer,
    )

    # Domain-kruu: EI liaisonia, EI AIMEAT-LLM-kutsuja -> ei rikki-JSON/NoneType.
    crew = Crew(
        agents=[researcher, analyst, writer],
        tasks=[research, analysis, writing],
        process=Process.sequential,
        verbose=True,
        cache=False,
    )
    result = crew.kickoff(inputs={"request": task["prompt"]})
    report = getattr(result, "raw", None) or str(result)

    # DETERMINISTINEN julkaisu + completaus (ei LLM:ää).
    _publish_and_complete(tid, memory_key, report)
    print("\n=== research-crew: TASKI KÄSITELTY ===", file=sys.stderr)


def run() -> None:
    llm = get_llm()

    # 1) Varmista Hello Integration kerran (liaison), jos kesken.
    if not _onboarding_completed():
        _run_onboarding_only(llm)
        if not _onboarding_completed():
            print(
                "[research-crew] VAROITUS: onboarding ei mennyt completed-tilaan; "
                "jatketaan silti pollaamista.",
                file=sys.stderr,
            )

    # 2) Poll-loop: halpa deterministinen jono-tarkistus; LLM-työ vain kun taski löytyy.
    interval = max(5, int(os.getenv("RESEARCH_CREW_POLL_SECONDS", "30")))
    print(
        f"[research-crew] Poll-loop käynnissä (väli {interval}s). Odotetaan AIMEAT-taskeja… "
        "(Ctrl+C lopettaa)",
        file=sys.stderr,
    )
    processed: set[str] = set()
    while True:
        task = _fetch_queued_task()
        if task and task.get("id") and task["id"] not in processed:
            print(f"[research-crew] Uusi taski jonossa: {task['id']} — käsitellään.", file=sys.stderr)
            try:
                _run_work(llm, task)
            except Exception as exc:  # noqa: BLE001
                print(f"[research-crew] Taskin {task['id']} käsittely epäonnistui: {exc}", file=sys.stderr)
            processed.add(task["id"])
        time.sleep(interval)


if __name__ == "__main__":
    run()
