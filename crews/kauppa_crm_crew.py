"""kauppa-crm: what was paid in the AIMEAT store appears in CADENCE as a customer and a won deal.

GTM phase 4. Once an hour this agent reads the store's paid sales (store.aimeat.io, ext:shop
crmFeed, read-only) and writes each one ONCE into CADENCE on aimeat.io: a contact with the store as
its source (or the contact CADENCE already has for that email, left as it is) and a won deal at the
price without VAT. The whole decision is code, `crewaimeat.kauppa_crm`; no model is asked anything.

It never writes to the store and never sends anything to anybody. A sale a person has already seen
in CADENCE is never written again, so edits made there stay.

Register + approve with exactly the scopes the work needs (read CADENCE, write and publish its
records), then restart the fleet:
  npx aimeat@latest connect --url https://aimeat.io --owner <you> --agent kauppa-crm \
    --mode task-runner --scopes organism:read,memory:write,organism:write
Two store credentials go in this machine's environment (the fleet's .env), never in git:
  KAUPPA_STORE_PAT=<a personal access token of the store account made for this>
  KAUPPA_CRM_FEED_TOKEN=<ext:shop's CRM_FEED_TOKEN>
Run one pass by hand:
  uv run python -c "from crewaimeat.kauppa_crm import sync_store_sales, describe; print(describe(sync_store_sales()))"
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.kauppa_crm import describe, sync_store_sales

AGENT_NAME = "kauppa-crm"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
LLM_PROFILE = "content-free"  # deterministic: on_task answers every task, no model call
RUN_MODE = "spawn"  # nothing to answer at once: one process per scheduled run, then gone
MAX_CONCURRENT = 1  # single-flight: two passes at once would both see a sale as missing
SCHEDULE = {
    "cron": "7 * * * *",
    "timezone": "Europe/Helsinki",
    "purpose": "reads the store's paid sales and writes the new ones into CADENCE as customers and won deals",
}
TAGS = ["crm", "cadence", "kauppa", "gtm", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "kauppa-crm", "type": "skill"}, {"name": "workspace-write", "type": "tool"}],
    "domain": [
        "carries paid AIMEAT store sales into CADENCE: a contact (source store.aimeat.io) and a won deal per sale",
        "written once per order; a person's own contact for the same email is referenced, never changed",
        "deterministic, no LLM; reads the store, never writes to it",
        "consumes:shop.crmFeed@1",
        "produces:crm-contacts",
        "produces:crm-deals",
    ],
    "languages": ["fi", "en"],
}

OFFERS = [
    {
        "id": "kauppa-cadenceen",
        "title": "Kirjaa kaupan maksetut myynnit CADENCEen",
        "ask": "Luen AIMEAT-kaupan maksetut myynnit ja kirjaan uudet CADENCEen: ostajasta asiakaskontaktin "
        "(lähde store.aimeat.io) ja kaupasta voitetun diilin verottomalla hinnalla. Jokainen myynti kirjataan "
        "kerran. Olemassa olevaa kontaktia en muuta, enkä kirjoita kauppaan mitään.",
        "example": "Kirjaa uudet kaupat CADENCEen",
        "cost": "free",
        "latency": "minutes",
        "tags": ["crm", "cadence", "kauppa"],
        "repeatability": "idempotent",
        "verification": "deterministic",
        "scheduleBorn": "hourly at :07 Europe/Helsinki — runs automatically",
        "consequences": [],
        "sample": "kauppa-crm: 3 sale(s) in the store; wrote 1 deal(s) and 1 new contact(s) into CADENCE; "
        "left out: 2 already in CADENCE",
    }
]

README = """[[FIGLET:slant]["Kauppa CRM"]]

Kun joku maksaa paikan AIMEAT-kaupassa (Founding-paikka, Solo, Team, Office, Own machine tai oma
palvelin), kirjaan sen CADENCEen: ostajasta asiakaskontakti, jonka lähde on store.aimeat.io, ja
kaupasta voitettu diili verottomalla hinnalla.

- Luen kaupan maksetut myynnit kerran tunnissa. Maksamaton tilaus ei ole myynti.
- Kirjaan jokaisen myynnin **kerran**. Jos muokkaat diiliä tai kontaktia CADENCEssa, en kirjoita sen päälle.
- Jos CADENCEssa on jo kontakti samalla sähköpostilla, liitän diilin siihen enkä muuta sitä.
- Testimaksuja en kirjaa.

En kirjoita kauppaan enkä lähetä kenellekään mitään.
"""


def build_domain(ctx: BuildContext):
    clerk = Agent(
        role="Store sales clerk",
        goal="Report the outcome of the deterministic store-to-CADENCE pass.",
        backstory="The pass is code: it reads the store's paid sales and writes the new ones into CADENCE. "
        "You never invent a sale, an amount or a customer.",
        llm=ctx.llm,
        tools=[],
    )
    task = Task(
        description=f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\nReport the outcome the pass produced.",
        agent=clerk,
        expected_output="One line: sales read, deals and contacts written, sales left out and why.",
    )
    return ([clerk], [task])


def run() -> None:
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.0,
            on_task=lambda task: describe(sync_store_sales(AGENT_NAME)),
        )
    )


if __name__ == "__main__":
    run()
