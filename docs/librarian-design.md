# Librarian — fleetin tietokartta, tuoreusvahti ja janitor

**Status:** suunnitelma + v1 toteutuksessa · **Päivä:** 2026-05-31

## Tarkoitus

Librarian on crew + scaffold-hook joka tekee fleetin tuottamasta tiedosta **löydettävää, tuoretta ja siistiä**:

1. **Indeksoija** — yleiskuva siitä mitä kukin agentti on tehnyt/hakenut, topic/domain-luokiteltuna.
2. **Reuse-before-redo** — ennen kallista työtä kysytään "onko tämä jo tehty?" → käytetään uudelleen ettei aja 58-haun tutkijaa turhaan.
3. **Tuoreusvahti** — arvioi kestääkö tieto aikaa vai happaneeko nopeasti; vanha fast-decay → "re-verify", ei sokea reuse.
4. **Arkistoija / janitor** — durability+TTL ohjaa: pidä / arkistoi Storageen tai knowledge-pakettiin / poista kylmästi. Pitää quotan ja indeksin laadun kurissa.

Kaikki nojaa **olemassa oleviin AIMEAT-primitiiveihin** (ei alustamuutoksia).

## Datalähteet (jo olemassa)

- `aimeat_memory_list owner_scope=true` → librarian (owner-agentti) näkee KAIKKI saman omistajan crewien entryt. Avaimet ovat jo crude-indeksi: `crews.<agent>.<slug>-<short>.latest_output`, `research.<agent>.<slug>…`, `agents.tag.<tag>.<run>.<crew>.<seq>`.
- `aimeat_agents_list` + `aimeat_agent_capabilities_report` → kunkin agentin julistetut kyvyt/domainit/tagit/kielet (reititystä varten).
- `aimeat_knowledge_*` → knowledge-paketit ("packages/") = alustan virallinen, JSON-strukturoitu koti aggregoidulle indeksille ja arkistolle.
- **Muistiarvot ovat JSON** (array+object) → harva avain isolla array-objektilla, ei avainräjähdystä.

## Quotat (vahdittavat rajat, 2026-05-31 nostettu)

| Raja | Arvo | Merkitys librarianille |
|---|---|---|
| Memory Quota / agentti | **100 MB** | lopullinen katto (kaikki avaimet+arvot) |
| `memory_max_keys_per_agent` | **10000** | avainmäärä — `.live`+`.latest_output` per task kasvattaa |
| `memory_max_value_size_kb` | **1024 KB (1 MB)** | yhden array-objekti-indeksin sitova raja → **shardaa** ennen 1 MB |

Periaate kautta linjan: **"pointers, not payloads"** — indeksi pitää avaimet + tiivisteet + metadatan; koko sisältö pysyy omassa deliverable-avaimessaan, luetaan vain tarvittaessa.

## Kaksi indeksikerrosta

**Kerros 1 — per-agentti library (agentti ylläpitää itse, scaffold-hook):**
Kompakti JSON-array yhdessä avaimessa `agents.<agent>.library`:
```json
[{"key":"crews.x.foo-ab12.latest_output","topic":"market-research",
  "sum":"board-game-cafe market ~$1.2B (2024), ~9.6% CAGR","durability":"fast",
  "ttl_days":120,"confidence":0.8,"ts":"2026-05-31"}]
```
- **v1:** kirjataan jokaisesta **deliverablesta** (task-end) yksi entry, kondensoitu + luokiteltu.
- **v2:** myös Tavily-hauista (vaatii web-toolin käärimisen, ks. alla).

**Kerros 2 — librarian aggregoi:**
Lukee `agents.*.library` + deliverable-avaimet (`owner_scope`) → mergeää **konsolidoiduksi fleet-indeksiksi** (topic/domain/agent-rollupit) → tallentaa knowledge-pakettiin `packages/fleet-index/*` tai `librarian.index`-avaimeen. Refresh ajoittain (cron) / pyydettäessä.

## Kestävyys- ja junk-luokitus (luotettavuuden ydin)

Jokainen pala luokitellaan **kondensointihetkellä** (sama halpa LLM-passi tuottaa kaiken):
```json
{ "keep": true, "topic": "fi-registry", "summary": "…",
  "durability": "permanent|slow|fast|ephemeral",
  "ttl_days": null, "confidence": 0.0-1.0 }
```

| Luokka | Esimerkki | Säilytys | Haussa |
|---|---|---|---|
| **permanent** | Suomi voitti MM-kullan 2022; Y-tunnus; perustamisvuosi | pidä, ei TTL | luotettava vuosienkin päästä |
| **slow** | hallitus, henkilöstömäärä, nyk. TJ | kuukausia, re-verify | "tarkista onko muuttunut" |
| **fast** | pörssikurssi, viimeisin rahoituskierros, päivän uutinen | lyhyt TTL | vanha → matala luottamus, hae uusiksi |
| **ephemeral / junk** | "ei löytynyt", kohina, blocked-page, hallusinaatio, off-topic | **EI indeksoida** (`keep:false`) | — |

**Junk-prefiltteri (deterministinen, ennen LLM:ää):** tyhjä/lyhyt arvo, "ei tuloksia"-markkerit, Tavily-relevanssi alle kynnyksen, boilerplate. → pudota halvalla ennen LLM-kutsua.

**Tuoreus haussa:** `effective_confidence = confidence × decay(ikä, durability)`. Fast-decay + vanha → palautetaan **"stale — re-verify"** eikä reuse.

## Scaffold-integraatio (leviää joka puolelle, kuten verificator)

Kaksi `CrewSpec`-lippua aimeat_crew.py:ssä:

1. **`contribute_to_library: bool`** (kuten `verify`): kun päällä, task-endin callback kondensoi+luokittelee deliverablen ja appendaa `agents.<agent>.library`-arrayyn (cap+dedupe). → kontribuutio leviää mihin tahansa crewiin lipulla.
2. **`consult_librarian`-työkalu**: annetaan koordinaattorille (`make_workflow_tools`) ja valinnaisesti muille → "onko tämä jo tehty?" ennen kallista työtä.

Raaka Tavily-haku-capture (kerros 1 v2): kääri `_web_tools()` → puskuroi `(query, tulos)` → task-endin callback kondensoi+luokittelee+appendaa libraryyn.

## Roolit (kaikki samalla luokitus-metadatalla)

1. **Indeksoija** — mitä on tehty/haettu, topic/domain.
2. **Reuse-vahti** — `consult_librarian(need)` → parhaat osumat + fit-arvio.
3. **Tuoreusvahti** — decay → "luotettava / re-verify".
4. **Janitor** — durability+TTL → pidä / arkistoi Storage/knowledge / poista; quota & laatu kurissa.

## Vaiheistus

- **v1 (nyt):** `librarian.py` (deterministinen gather + score + durability-tietoinen haku) + `consult_librarian`-työkalu + kestävyys/junk-luokitus kondensoinnissa + `librarian_crew.py` (ohut crew) + `CrewSpec.contribute_to_library`-lippu (deliverable→library).
- **v2:** Tavily-haku-capture (web-tool wrap) → per-haku-library.
- **v3:** librarian-aggregointi knowledge-pakettiin + refresh (cron) + janitor (arkistoi+poista) + per-agentti käyttö vs quota -raportti.

## Riskit / huomiot

- Pisteytyksen laatu: keyword+recency+LLM-rerank riittää v1:een; embeddings myöhemmin.
- LLM-luokitus per kandidaatti = kustannus → v1 luokittelee vain **kärkikandidaatit** hakuhetkellä (lazy), ei koko indeksiä.
- owner_scope-luku = vain saman omistajan näkymä (henkilökohtainen fleet) — ei cross-owner.
- Indeksin koko: shardaa ennen 1 MB/arvo; cap per-agentti library; rollup vanhat.
