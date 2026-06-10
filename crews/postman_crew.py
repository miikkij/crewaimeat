"""postman: email-out as a workspace contract + the 07:00 morning report.

Layer 1 — any agent gets email by writing ONE `mail-request` record ({subject, body_md, image_key?});
postman sends it over SMTP with the AIMEAT_MAIL_TO allowlist enforced on every send (a recipient not
on the list is refused — the structural spam risk is zero). Sending is plain smtplib, zero LLM.

Layer 2 — every morning at 07:00 (Europe/Helsinki) the idle hook composes yesterday's digest:
the organism-wide activity delta (who did what, distilled), the SOME radar's fresh opportunities,
and a day-brightening image (SearXNG + qwen-vl pick) — and emails it. Once per day, dedup by the
morning-<date> record's existence (restart-safe).

Quick test (after registering):
  uv run python -c "from crewaimeat.mail_contract import process_mail; print(process_mail())"

Run as a crew:
  npx aimeat@latest connect add --agent postman --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/postman_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.contract_adopt import build_adopt_domain, is_adopt_task
from crewaimeat.mail_contract import CONTRACT, idle_pass, make_mail_tools

AGENT_NAME = "postman"

README = '''[[FIGLET:slant]["Postman"]]

Email-out as a **workspace contract**: write a `mail-request` record ({subject, body_md, image_key?})
and I send it over SMTP — with a **recipient allowlist enforced on every send** (anything off-list is
refused). Sending is deterministic smtplib, zero LLM. I also compose and send the **07:00 morning
report**: yesterday's organism activity (distilled), the SOME radar's fresh finds, and a
day-brightening image picked by a vision model. Once per day, restart-safe.

**How to task me:** "send" — I run process_mail ONCE and send any pending mail-requests.
'''


def build_domain(ctx: BuildContext):
    if is_adopt_task(ctx.task):  # UI "Adopt contract" chip -> provision the mail-request space there
        return build_adopt_domain(ctx, AGENT_NAME, CONTRACT)
    courier = Agent(
        role="Mail Courier",
        goal="Send pending mail-request records over SMTP, allowlist-enforced.",
        backstory="You deliver workspace mail: you call process_mail EXACTLY ONCE and report its "
                  "result. The tool enforces the recipient allowlist; you never compose recipients "
                  "yourself and never send anything outside the records.",
        llm=ctx.llm,
        tools=[*make_mail_tools(AGENT_NAME)],
    )
    send_task = Task(
        description=(f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
                     "Call process_mail() EXACTLY ONCE. It deterministically sends any pending "
                     "mail-request records (allowlist-enforced). Report the counts."),
        agent=courier,
        expected_output="The process_mail report: how many mails were sent.",
    )
    return ([courier], [send_task])


def run() -> None:
    # idle_hook: clock check for the 07:00 morning report (record-existence dedup) + the pending
    # mail sweep. The CHECK is plain code; vision/distill run only when a report is actually due.
    def _poll() -> None:
        res = idle_pass()
        if res.get("sent") or res.get("failed"):
            print(f"[{AGENT_NAME}] mail poll: {res}")

    run_crew(CrewSpec(
        agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README,
        temperature=0.3, idle_hook=_poll, idle_hook_seconds=120,
    ))


if __name__ == "__main__":
    run()
