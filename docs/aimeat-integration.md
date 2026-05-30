# crewaimeat ↔ AIMEAT -integraatio

Miten crewaimeat-kruut kytketään **AIMEAT**-noodiin. Kaksi tapaa; **Liaison Agent**
on suositeltu.

> Syvempi teoria ja framework-agnostinen vastaavuuskartta: AIMEATin oma
> [`docs/integrations/crewai.md`](https://github.com/miikkij/aimeat-protocol/blob/main/docs/integrations/crewai.md).

## crewaimeatn rakenne

| Kruu | Entrypoint | Prosessi | Agentit |
|------|-----------|----------|---------|
| **Company** | `crewaimeat.runner` | hierarkinen | CEO (manageri) + CTO, CMO, CFO, COO |
| **Kevyt** | `crewaimeat.demo` | sekventiaalinen | researcher → analyst → writer |

Roolit: [src/crewaimeat/config/agents.yaml](../src/crewaimeat/config/agents.yaml). LLM:
OpenRouter/xAI ([src/crewaimeat/llm.py](../src/crewaimeat/llm.py)). Web-haku: Tavily.

## Tapa A — AIMEAT Liaison Agent (suositus)

`aimeat-crewai`-paketti tarjoaa **liaison-agentin**, joka lisätään kruuhun. Se hoitaa
KAIKEN AIMEAT-kommunikaation (Hello Integration, capabilities, task-elinkaari,
memory, telemetria) MCP-pinnan kautta — muut agentit tekevät vain domain-työnsä.

```python
from crewai import Crew, Task
from aimeat_crewai import create_liaison_agent, stdio_params
from crewaimeat.llm import get_llm

params = stdio_params(agent_name="company-crew")   # spawnaa `aimeat connect serve`
with create_liaison_agent(mcp_server_params=params, agent_name="company-crew",
                          llm=get_llm(), verbose=True) as liaison:
    crew = Crew(agents=[liaison, ...], tasks=[...])
    crew.kickoff()
```

Toimiva esimerkki: [try_liaison.py](../try_liaison.py).

### Ajo tyhjästä asennuksesta

```powershell
# 1) crewaimeat-riippuvuudet + liaison-paketti
python -m uv sync
python -m uv pip install aimeat-crewai

# 2) AIMEAT-connector (globaali)
npm install -g aimeat@latest

# 3) Rekisteröi kruu AIMEAT-agentiksi task-runner-moodissa
aimeat connect add --agent company-crew --mode task-runner --url https://aimeat.io --owner <owner>
#   -> hyväksy selaimessa: <node>/v1/agents/verify  (Profile -> Agents)

# 4) Täytä avaimet
copy .env.example .env   # OPENROUTER_API_KEY, TAVILY_API_KEY

# 5) Aja liaison-demo
uv run python try_liaison.py
```

Verifioi serveriltä: `aimeat connect call aimeat_onboarding_status --agent company-crew --json '{}'`
→ `status: completed`, 7 askelta passed.

### Mitä liaison kirjoittaa AIMEATiin

| Avain / kohde | Sisältö |
|---------------|---------|
| `agents.config.<agent>.runtime` | `{ runtime: "crewai", version: <crewai-versio> }` (publish_config-askel) |
| Onboarding test task | merkitään `done` (accept + complete) |
| (capabilities, telemetria) | raportoidaan onboardingin yhteydessä |

> `crewaimeat.runner` (task-runner-subprocess, ks. Tapa B) kirjoittaa lisäksi
> best-effort -muistiinpanot avaimiin `crews/company/tasks/<task_id>/started`
> ja `.../result` ([src/crewaimeat/aimeat.py](../src/crewaimeat/aimeat.py)).

### Suositukset (tuotanto)
- **`tool_filter`**: liaison saa oletuksena ~95 MCP-työkalua (mm. wallet/admin/consent).
  Rajaa tarpeellisiin: `create_liaison_agent(..., tool_filter=[...])`.
- Merkitse yksi agentti `primary: true` (`~/.aimeat/agents/<agent>/config.yaml`)
  poistaaksesi serven "no primary" -varoituksen.

## Tapa B — task-runner-subprocess

`aimeat connect serve` käynnistää crewaimeat-kruun aliprosessina kun tehtävä saapuu;
kruu lukee env-muuttujat, ajaa, tulostaa JSON-deliverablen stdoutiin. Per-agent
config + env-sopimus: ks. [examples/aimeat/](../examples/aimeat/). Tämä sopii
yksinkertaisiin fire-and-forget -keisseihin; LLM-pohjaisille kruuille Liaison
(Tapa A) on parempi.

## CrewAI ↔ AIMEAT -käsitevastaavuudet (tiivis)

| CrewAI | AIMEAT |
|--------|--------|
| Tools | toiminta-MCP-työkalut (task_*, message_*, board_*, capabilities_invoke, action_execute …) |
| Knowledge (RAG) | memory_read/search, knowledge_get/list, storage_download, handbook_get (custom `BaseKnowledgeSource`) |
| Memory | `memory_*` (pysyvä jaettu muisti) |
| Skills (SKILL.md) | skill-bundle `~/.aimeat/<agent>/SKILL.md` + handbook (ks. tilahuomio alla) |
| Crew / Flow | task- ja work-elinkaari (`task_*`, `work_*`) |
| Delegointi | `capabilities_invoke`, `organism_*`, `catalogue_*` |

## Tila
- ✅ Liaison-pattern toimii päästä päähän (AIMEAT 1.13.2+, aimeat-crewai 0.1.2+).
- 🚧 Natiivi CrewAI **Skills** -tuki (`Agent(skills=[SKILL.md])`) odottaa AIMEATin
  skill-bundlen frontmatter- ja hakemistorakenteen yhteensopivuutta (työn alla,
  aimeat-crewai 0.2.0).
