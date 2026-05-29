# crewfive

Proof of Concept: **5 esimääriteltyä agenttia hierarkisessa CrewAI-kruussa**, jotka
"pyörittävät yritystä" johtoryhmänä. Halpa ajo OpenRouterin (tai suoraan xAI/Grokin)
kautta, web-haku Tavilylla. Annat tehtävän → CEO delegoi osastopäälliköille →
koostettu raportti tallentuu levylle.

## Agentit (hierarkia)

| Rooli | Tehtävä | Manageri? |
|-------|---------|-----------|
| **CEO** | Delegoi ja koostaa lopputuloksen | ✅ `manager_agent` |
| **CTO** | Teknologia, arkkitehtuuri, toteutettavuus | työntekijä |
| **CMO** | Markkinointi, brändi, go-to-market | työntekijä |
| **CFO** | Talous, budjetti, kannattavuus | työntekijä |
| **COO** | Operaatiot, prosessit, toteutus | työntekijä |

CrewAI:n **hierarkisessa prosessissa** (`Process.hierarchical`) taskeja ei sidota
agentteihin – manageri (CEO) päättää kuka tekee mitäkin, haastaa tulokset ja koostaa
ne. Roolikuvaukset (role/goal/backstory) ovat tiedostossa
[src/crewfive/config/agents.yaml](src/crewfive/config/agents.yaml) ja taskimäärittely
[src/crewfive/config/tasks.yaml](src/crewfive/config/tasks.yaml).

## Vaatimukset

- [uv](https://docs.astral.sh/uv/) (tällä koneella `python -m uv`)
- API-avaimet:
  - **OpenRouter** (pakollinen oletuksena) – https://openrouter.ai/keys
  - **Tavily** (vapaaehtoinen, web-hakua varten) – https://app.tavily.com/

## Asennus

```powershell
# 1) Asenna riippuvuudet (uv luo .venv:n automaattisesti)
python -m uv sync

# 2) Tee .env ja täytä avaimet
copy .env.example .env
notepad .env
```

`.env`-tiedostoon vähintään:

```dotenv
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=openrouter/x-ai/grok-4-fast
TAVILY_API_KEY=tvly-...        # vapaaehtoinen
```

## Käyttö

```powershell
# Anna tehtävä argumenttina:
python -m uv run crew "Laadi Q3 markkinointisuunnitelma uudelle mobiilipelille"

# Tai aja oletusdemo ilman argumenttia:
python -m uv run crew
```

> Kun `uv` on PATH:issa, voit ajaa lyhyemmin `uv run crew "..."`.

Tulokset tallentuvat kansioon [output/](output/):
- `raportti_<aikaleima>.md` – luettava johtoryhmän raportti
- `raportti_<aikaleima>.json` – lopputulos + jokaisen taskin tuotos + token-käyttö

## Mallin / tarjoajan vaihto

Kaikki ohjataan `.env`:stä – koodia ei tarvitse muokata.

**OpenRouter, halpoja malleja:**
```dotenv
OPENROUTER_MODEL=openrouter/deepseek/deepseek-chat-v3.1   # erittäin halpa
OPENROUTER_MODEL=openrouter/google/gemini-2.0-flash-001   # halpa, nopea
OPENROUTER_MODEL=openrouter/openai/gpt-4o-mini            # luotettava
```

**xAI (Grok) suoraan, ilman OpenRouteria:**
```dotenv
USE_XAI=1
XAI_API_KEY=xai-...
XAI_MODEL=xai/grok-4-fast
```

## Rakenne

```
crewfive/
├─ pyproject.toml              # riippuvuudet (crewai[tools], tavily, dotenv)
├─ .env.example               # ympäristömuuttujien malli
├─ src/crewfive/
│  ├─ main.py                 # CLI: kickoff + tulosten tallennus
│  ├─ crew.py                 # hierarkinen kruu, CEO = manager_agent
│  ├─ llm.py                  # LLM-factory (OpenRouter / xAI)
│  └─ config/
│     ├─ agents.yaml          # 5 roolin kuvaukset
│     └─ tasks.yaml           # taskimäärittely ({request} CLI:stä)
└─ output/                    # ajojen tulokset (md + json)
```

## Miten hierarkia toimii (lyhyesti)

1. `main.py` lukee tehtävän CLI:stä ja kutsuu `crew.kickoff(inputs={"request": ...})`.
2. `{request}` korvautuu taskin kuvaukseen ([tasks.yaml](src/crewfive/config/tasks.yaml)).
3. **CEO-manageri** analysoi direktiivin, delegoi osat CTO/CMO/CFO/COO:lle,
   pyytää tarvittaessa web-hakua (Tavily) ja koostaa lopputuloksen.
4. Lopputulos + osatuotokset tallennetaan `output/`-kansioon.

## Lähteet

- CrewAI – LLMs: https://docs.crewai.com/en/concepts/llms
- CrewAI – Hierarchical Process: https://docs.crewai.com/how-to/hierarchical-process
- CrewAI – Tavily Search Tool: https://docs.crewai.com/en/tools/search-research/tavilysearchtool
