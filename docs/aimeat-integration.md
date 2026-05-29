# AIMEAT-integraatio: crewfive task-runnerina

Tämä dokumentti kuvaa, miten crewfive-kruut kytketään **AIMEATiin** task-runnereina.

## Mitä integraatio tekee

`aimeat connect serve` voi käynnistää crewfive-kruun **aliprosessina** kun agentille
saapuu tehtävä. Serve antaa tehtävän env-muuttujina, kruu ajaa, tulostaa lopputuloksen
yhtenäisenä **Deliverable-JSON:na**, ja serve postaa sen takaisin AIMEATiin tehtävän
valmistumiseksi. Käyttäjä (esim. Claude Desktopissa) näkee vain alun ja lopputuloksen –
ei CrewAI:n sisäistä agenttikeskustelua.

## Miksi ei Python-pakettia

Integraatio **ei** vaadi AIMEAT-Python-pakettia eikä riippuvuutta crewfiveen.
Pelkkä **subprocess + CLI** riittää:
- Serve → kruu: env-muuttujat ja stdout/tiedosto.
- Kruu → AIMEAT (valinnainen): komentorivikutsu `aimeat connect call <tool> --json '{...}'`.

## Env-sopimus

Serve asettaa aliprosessille nämä muuttujat (crewfive lukee ne moduulissa
[src/crewfive/aimeat.py](../src/crewfive/aimeat.py) funktiolla `read_runner_env()`):

| Muuttuja | Merkitys |
|----------|----------|
| `AIMEAT_TASK_PROMPT` | Tehtävän kuvaus (käytetään kruun inputtina) |
| `AIMEAT_TASK_ID` | Tehtävän id (käytetään muistiinpanon avaimissa) |
| `AIMEAT_AGENT_NAME` | Agentin nimi |
| `AIMEAT_TOKEN` | Token (varattu tulevaa käyttöä varten) |

Jos `AIMEAT_TASK_PROMPT` puuttuu, skriptit käyttävät CLI-argumenttia tai oletustehtävää,
joten ne ovat ajettavissa myös standalone-testinä.

## Deliverable-muoto

Molemmat kruut tulostavat saman JSON-rakenteen:

```json
{
  "title": "...",
  "summary": "...",
  "sections": [{ "heading": "...", "content": "..." }],
  "recommendations": ["..."]
}
```

- `CREW_OUTPUT_FILE`-env asetettuna → JSON kirjoitetaan myös tuohon tiedostoon
  (`output_capture: file:<path>`).
- JSON tulostuu aina myös stdoutin viimeiseksi (`output_capture: stdout`).

## Kaksi kruua

| Kruu | Entrypoint | Prosessi | Avaimet | output_capture |
|------|-----------|----------|---------|----------------|
| **Kevyt** (3 agenttia) | `python -m crewfive.demo` | sequential | OpenRouter + Tavily (crewfiven `.env`) | `stdout` |
| **Company** (5 agenttia) | `python -m crewfive.runner` | hierarchical (CEO delegoi) | OpenRouter + Tavily (crewfiven `.env`) | `file:<path>` |

Molemmat ovat aitoja toteutuksia: oikea LLM (`get_llm()`) ja oikea Tavily-haku (jos
`TAVILY_API_KEY` asetettu). Kevyt kruu on suoraviivainen sekventiaalinen putki
(researcher → analyst → writer); company-kruu on hierarkinen ja tuottaa runsaammin
välitulostetta → suositellaan tiedostokaappausta puhtaan JSON:n saamiseksi.

### Standalone-testaus (ilman serveä)

```powershell
# Kevyt 3 agentin kruu (lukee avaimet crewfiven .env:stä):
$env:AIMEAT_TASK_PROMPT="Tee pieni markkinointisuunnitelma"
uv run python -m crewfive.demo

# Company-kruu:
$env:AIMEAT_TASK_PROMPT="Laadi Q3 go-to-market-suunnitelma"
$env:CREW_OUTPUT_FILE="output/deliverable.json"
uv run python -m crewfive.runner
```

## Per-agent config (serve)

Esimerkit kansiossa [examples/aimeat/](../examples/aimeat/):
- [demo-crew.config.yaml](../examples/aimeat/demo-crew.config.yaml)
- [marketing-crew.config.yaml](../examples/aimeat/marketing-crew.config.yaml)

Sijoitetaan tiedostoon `~/.aimeat/agents/<agent>/config.yaml` (kun serven
task-runner-moodi on julkaistu). **Turvavaroitus:** `runner.command` exec'ataan
sellaisenaan – luota vain omaan `~/.aimeat/`-sisältöösi (sama foot-gun kuin
`wake.command`).

## Miten kruu kutsuu AIMEATia takaisin

Kesken ajon kruu voi kirjoittaa muistiinpanon AIMEATiin (best-effort) funktiolla
`write_memory_note()` ([aimeat.py](../src/crewfive/aimeat.py)), joka ajaa:

```bash
aimeat connect call aimeat_memory_write --json '{"key":"...","value":{...},"visibility":"private","tags":[...]}'
```

`aimeat_memory_write`-parametrit: pakolliset `key` (string) ja `value` (mikä tahansa
JSON), valinnaiset `visibility` (`private|owner|group|public`), `ttl_hours` (number),
`tags` (array). Jos `aimeat`-CLI:tä ei löydy PATHista tai kutsu epäonnistuu, se
ohitetaan eikä kruun ajoa kaadeta. Autentikointi tapahtuu AIMEAT-CLI:n omasta
konfiguraatiosta (`~/.aimeat/`), ei tästä prosessista.

## Useamman kruun rekisteröinti

Jokainen kruu on **oma AIMEAT-agenttinsa**. Liitä lisää agentteja, anna kullekin oma
`~/.aimeat/agents/<agent>/config.yaml`, ja osoita `runner.args` haluttuun entrypointiin
(`crewfive.demo`, `crewfive.runner`, tai oma `crewfive.<moduuli>`).

## Troubleshooting

| Oire | Syy / korjaus |
|------|---------------|
| stdout sisältää JSON:n lisäksi kohinaa | Käytä `output_capture: file:<path>` (company-kruu) tai aseta `CREW_VERBOSE=0`. |
| `OPENROUTER_API_KEY puuttuu` | Täytä crewfiven `.env` (tai aseta `USE_XAI=1` + `XAI_API_KEY`). |
| `'aimeat'-CLI ei löydy` (stderr) | Vain best-effort-muistiinpano ohittui; ei estä lopputulosta. Asenna/kytke `aimeat` jos haluat takaisinkutsut. |
| Ääkköset rikki konsolissa | Skriptit pakottavat stdoutin UTF-8:ksi; tiedostot kirjoitetaan aina UTF-8:na. |
| Exit-koodi ≠ 0 | Kruu kaatui; serve merkitsee tehtävän epäonnistuneeksi (`aimeat_task_fail`). Katso stderr. |

## Tila

`aimeat connect serve`:n **task-runner-moodia ei ole vielä julkaistu** AIMEATissa
(spec: `aimeat-protocol/docs/.../2026-05-29-crewai-task-runner-and-multi-agent-serve.md`).
crewfiven puoli on kuitenkin valmis sitä varten, ja takaisinkutsu (`aimeat connect call`)
toimii jo nyt nykyisellä `aimeat connect serve`:llä.
