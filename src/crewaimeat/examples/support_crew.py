"""A three-agent crew that triages an incoming customer support issue, verifies the facts and policy, and drafts a clear, empathetic reply while flagging anything that needs human escalation.

Generated example crew on the AIMEAT scaffold. Edit build_domain to taste;
the scaffold provides the AIMEAT wiring (see SCAFFOLD_CANON.md). Register first:
  aimeat connect add --agent support-crew --mode task-runner --url https://aimeat.io --owner <owner>

Run: python -m crewaimeat.examples.support_crew
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew

AGENT_NAME = "support-crew"


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    triager = Agent(
        role="Support Triage Specialist",
        goal="Classify the incoming customer issue by type, urgency, and sentiment, and pinpoint exactly what the customer is asking for.",
        backstory="You are an experienced front-line support lead who rapidly reads a customer message, separates the real problem from the noise, and judges its severity and emotional tone. You never invent details that the customer did not provide.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )
    resolver = Agent(
        role="Resolution Analyst",
        goal="Determine the correct, accurate resolution for the triaged issue and decide whether it can be answered directly or must be escalated or flagged.",
        backstory="You are a meticulous support analyst who maps each issue to the right answer or next step, citing only information actually present in the request. You clearly mark any claim you cannot verify and recommend escalation when account access, refunds, legal, security, or unclear policy is involved.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )
    writer = Agent(
        role="Customer Reply Writer",
        goal="Write a clear, accurate, empathetic reply to the customer based on the triage and resolution, and surface any escalation flags for the support team.",
        backstory="You are a professional customer-support writer who turns internal analysis into a warm, concise, jargon-free message that acknowledges the customer's feelings, answers their question, and sets honest expectations about next steps.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )

    triage = Task(
        description="Read the incoming customer support message below carefully. Identify and summarize: (1) the customer's core problem or request in one or two sentences, (2) the issue category (for example billing, technical, account access, shipping, product question, complaint, other), (3) the urgency level (low, medium, high) with a brief reason, and (4) the customer's sentiment and tone. List any important details the customer provided (order numbers, dates, product names, error messages) and explicitly note any critical information that is missing and would be needed to fully resolve the issue. Do not invent or assume facts the customer did not state." + f"\n\nRequest:\n{ctx.prompt}",
        expected_output="A short structured triage summary: core problem, category, urgency with reason, sentiment, key details provided, and any missing information.",
        agent=triager,
    )
    resolve = Task(
        description="Using the triage summary, determine the best resolution for the customer's issue. Provide: (1) the recommended answer or set of steps that addresses the problem, based only on information available in the triage and request, (2) a clear judgment of whether this can be resolved in a direct reply or must be ESCALATED to a human or specialist team, and (3) any FLAGS that the support team must see (for example: requires account access or identity verification, involves a refund or payment dispute, raises a security, privacy, legal, or safety concern, or relies on policy that is unclear or unverifiable). For every recommendation, mark plainly whether it is verified from the request or an assumption that needs confirmation. If essential information is missing, state the specific clarifying questions to ask the customer instead of guessing.",
        expected_output="A resolution plan listing the recommended answer/steps, an escalate-or-reply decision, explicit escalation flags, verified-vs-assumed markings, and any clarifying questions needed.",
        agent=resolver,
    )
    draft_reply = Task(
        description="Write the final customer-facing reply based on the triage summary and the resolution plan. The reply must: open by acknowledging the customer's situation and emotion with genuine empathy, clearly and accurately answer their question or explain the next steps, set honest expectations (including, if applicable, that the matter is being escalated to a specialist and roughly what happens next), and close warmly and professionally. Use plain, friendly language with no internal jargon. Do not promise anything the resolution plan did not support, and do not state any detail marked as unverified as if it were confirmed. After the customer reply, add a short separated INTERNAL NOTE section for the support team that lists the issue category, urgency, and any escalation flags or clarifying questions identified in the resolution plan. Follow any explicit instructions in the original customer request below regarding language, tone, or format; otherwise choose what fits best." + f"\n\nRequest:\n{ctx.prompt}",
        expected_output="A complete customer-facing reply (empathetic opening, accurate answer/next steps, honest expectations, warm close) followed by a clearly separated internal note with category, urgency, and escalation flags.",
        agent=writer,
    )

    return [triager, resolver, writer], [triage, resolve, draft_reply]


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain))


if __name__ == "__main__":
    run()
