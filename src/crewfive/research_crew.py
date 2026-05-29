"""research-crew — liaison + 3 domain-agenttia, onboarding-gatella.

Logiikka (käyttäjän malli):
- Jos Hello Integration EI ole tehty -> aja VAIN onboarding (liaison yksin),
  EI sekoiteta domain-agentteja mukaan.
- Kun onboarding on 'completed' -> HAE oikea queued-taski AIMEATista. Jos taski
  löytyy: domain-agentit (Tutkija -> Analyytikko -> Kirjoittaja) käsittelevät
  VAIN sen, ja liaison completaa sen AIMEAT-taskin + julkaisee tuloksen muistiin
  TASKIIN SIDOTTUUN avaimeen (oikea task-id). Jos jonossa ei ole taskia ->
  ei tehdä mitään (tehtäviä ei keksitä).

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
# --------------------------------------------------------------------------- #
def _aimeat_call(tool: str, payload: dict) -> dict | None:
    """Kutsuu `aimeat connect call <tool>` ja palauttaa JSON-dictin (tai None)."""
    if shutil.which("aimeat") is None:
        return None
    base = ["aimeat", "connect", "call", tool, "--agent", AGENT_NAME, "--stdin"]
    cmd = ["cmd", "/c", *base] if os.name == "nt" else base
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(payload), capture_output=True, text=True, timeout=60
        )
        return json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        print(f"[research-crew] {tool} epäonnistui: {exc}", file=sys.stderr)
        return None


def _onboarding_completed() -> bool:
    data = _aimeat_call("aimeat_onboarding_status", {})
    return bool(data) and data.get("onboarding", {}).get("status") == "completed"


def _fetch_queued_task() -> dict | None:
    """Hakee vanhimman jonossa olevan (queued) taskin AIMEATista.

    Palauttaa {id, title, prompt} tai None jos jonossa ei ole tehtäviä.
    """
    data = _aimeat_call("aimeat_task_list", {"status": "queued", "per_page": 10})
    if not data:
        return None
    tasks = data.get("tasks", [])
    if not tasks:
        return None
    t = tasks[0]  # vanhin queued
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
# Tila 1: pelkkä Hello Integration (liaison yksin)
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
                "3. For the test task: call aimeat_task_propose_todos ONCE to create the "
                "TODOs.\n"
                "4. Mark the TODOs done with aimeat_task_todo ONE AT A TIME — one call, "
                "wait for its result, then the next. Never several in the same turn "
                "(parallel updates race and lose writes).\n"
                "5. Once all TODOs are done, you MUST call aimeat_task_complete with the "
                "test task's id to complete the test task. This single call is what makes "
                "the 'complete_test_task' step pass — the task is NOT finished until you "
                "call it. Do NOT re-mark TODOs that are already done.\n"
                "6. Call aimeat_onboarding_status once more and report the final state.\n"
                "Do NOT do any domain work or research."
            ),
            expected_output=(
                "All 7 onboarding steps passed (status 'completed'); the test task was "
                "completed via aimeat_task_complete."
            ),
            agent=liaison,
        )
        Crew(
            agents=[liaison],
            tasks=[task],
            process=Process.sequential,
            verbose=True,
            cache=False,  # ei stale tool-cachea -> ei re-mark-looppia
        ).kickoff()
        print("\n=== research-crew: ONBOARDING-ONLY valmis ===")


# --------------------------------------------------------------------------- #
# Tila 2: käsittele YKSI oikea queued-taski (domain-agentit + liaison completaa)
# --------------------------------------------------------------------------- #
def _run_work(llm, task: dict) -> None:
    memory_key = _memory_key(task)
    tid = task["id"]
    print(
        f"[research-crew] Onboardattu. Käsitellään AIMEAT-taski {tid}\n"
        f"[research-crew] Prompt: {task['prompt']}\n"
        f"[research-crew] Julkaisuavain: {memory_key}",
        file=sys.stderr,
    )
    with _make_liaison(llm) as liaison:
        researcher = Agent(
            role="Tutkija",
            goal="Kerää relevantti, ajantasainen taustatieto tehtävään: {request}",
            backstory=(
                "Olet perusteellinen tutkija, joka kokoaa faktat ja lähteet ennen "
                "analyysiä. Käytät web-hakua varmistaaksesi ajantasaisuuden."
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
        finalize = Task(
            description=(
                "Take the writer's final result. Verify facts via tool responses; do not "
                "assume.\n"
                f"1. Write the result to AIMEAT memory under the EXACT key '{memory_key}' "
                "with visibility owner (aimeat_memory_write). Confirm the write succeeded.\n"
                f"2. Mark the AIMEAT task '{tid}' as done. If the task is not yet active, "
                "accept/start it first (e.g. propose TODOs and mark them done one at a time, "
                "verifying each via aimeat_task_get), then call aimeat_task_complete with "
                "the writer's result as the completion summary.\n"
                f"3. Re-fetch the task with aimeat_task_get and confirm its status is 'done'."
            ),
            expected_output=(
                f"Confirmation: memory written to '{memory_key}' and AIMEAT task '{tid}' "
                "verified 'done'."
            ),
            agent=liaison,
        )

        crew = Crew(
            agents=[liaison, researcher, analyst, writer],
            tasks=[research, analysis, writing, finalize],
            process=Process.sequential,
            verbose=True,
            cache=False,  # ei stale tool-cachea
        )
        result = crew.kickoff(inputs={"request": task["prompt"]})
        print("\n=== research-crew RESULT ===")
        print(getattr(result, "raw", result))


def run() -> None:
    llm = get_llm()

    # 1) Varmista Hello Integration kerran (liaison yksin), jos se on kesken.
    if not _onboarding_completed():
        _run_onboarding_only(llm)
        if not _onboarding_completed():
            print(
                "[research-crew] VAROITUS: onboarding ei mennyt completed-tilaan; "
                "jatketaan silti pollaamista.",
                file=sys.stderr,
            )

    # 2) Jää pollaamaan jonoa. Pollaus on halpaa (deterministinen, EI LLM:ää);
    #    LLM-työ tehdään vain kun queued-taski löytyy. Väli env:stä (oletus 30 s).
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
            except Exception as exc:  # noqa: BLE001 – yksi taski ei saa kaataa looppia
                print(f"[research-crew] Taskin {task['id']} käsittely epäonnistui: {exc}", file=sys.stderr)
            processed.add(task["id"])
        time.sleep(interval)


if __name__ == "__main__":
    run()
