# Dynaaminen monitoimija-koordinaatio — sessioraportti

**Päivä:** 2026-05-31 · **Projekti:** crewaimeat (AIMEAT × CrewAI starter kit) · **Owner:** happydude500001
**Versiot:** connector `npx aimeat@latest` = 1.14.6 · `aimeat-crewai` = 0.3.6

Tämä raportti dokumentoi mitä **kokeiltiin**, mitä **todettiin todeksi**, **vaiheet**, **välivaiheiden tulokset** (lyhyesti) ja **lopputulokset** (täydet deliverablet liitteessä). Tavoite oli todistaa, että `workflow-manager` (koordinaattori-crew) + `crew-forge` (crewejä rakentava crew) muodostavat järjestelmän joka **kasvattaa itseään, ketjuttaa riippuvuuksia, haarautuu ajossa ja noudattaa ihmisen asettamia direktiivejä** — kaikki olemassa olevilla AIMEAT-primitiiveillä.

---

## 1. Lähtökohta ja arkkitehtuuri

**Malli: litteä orkestraattori–worker** (tutkimuksen "sweet spot", ks. §7). Koordinaattori ei koskaan tee domain-työtä itse — se löytää crewit, delegoi, kerää ja syntetisoi. Workerit ovat eristettyjä task-runner-crewejä jotka eivät näe toisiaan eivätkä koko tavoitetta.

**Keskeiset komponentit:**

| Tiedosto | Vastuu |
|---|---|
| `src/crewaimeat/aimeat_crew.py` | Lukittu scaffold: `run_crew`, `CrewSpec`, `BuildContext`, deterministinen publish/complete, single-instance-lukko, direktiivien luku, **task-nature-gate** (`adapt_to_task` → temppi/grounding/verify), **verify** (completeness/factcheck), **contribute_to_library**, README-julkaisu, daemon-kytkentä |
| `src/crewaimeat/workflow.py` | Koordinaattorin työkalut: `discover_crews`, `delegate_subtask`, `delegate_and_wait`, `collect_results`, `commission_crew`, `wait_for_crew`, **`ask_owner`** + shared-tag-luku |
| `src/crewaimeat/forge.py` | crew-forgen koneisto: `write_and_validate_crew`, `register_agent` (device-koodi), `register_and_launch_crew`, `reconcile_fleet`, restart/reauth/list/start_all |
| `src/crewaimeat/librarian.py` | Tietokartta: `gather_deliverables`, `classify_entry` (kestävyys/junk), `search_index`, `consult_librarian`, `contribute_deliverable` |
| `crews/workflow_manager_crew.py` | Koordinaattori-crew (dispatcher + Editor); `adapt_to_task=True` |
| `crews/crew_forge_crew.py` | "Agentti joka tekee agentteja" |
| `crews/librarian_crew.py` | Tietokartta-crew (reuse-before-redo, tuoreusvahti) |
| `crews/*_crew.py` | Domain-crewit: joker, probability-creator, sanity-checker, idea-feasibility-rater, tagline-translator, jingle-writer |

**Shared-tag-mekanismi (fan-in):** worker julkaisee deliverablensa avaimeen `agents.tag.workflow.<run_id>.<crew>.<seq>`, jonka koordinaattori lukee OMALLA tokenillaan (`memory_list owner_scope=true`). `run_id` = koordinaattorin task-id:n ensimmäinen segmentti → tulokset ovat task-id:llä tunnistettavissa. `<seq>` = per-run-laskuri (sama crew voi esiintyä putkessa törmäämättä).

**Deterministinen julkaisu + complete:** scaffold kirjoittaa viimeisen domain-taskin tuotoksen muistiin callbackilla ja sulkee taskin koodilla — ei luota LLM-liaisonin muistikirjoitukseen (joka heikolla mallilla loopasi). Lopullinen deliverable: `crews.<agent>.<slug>-<short>.latest_output`.

---

## 2. Keissi #1 — Dynaaminen kyvykkyyden hankinta (commission)

**Mitä kokeiltiin:** kaksikielinen "Brewmaster"-lautapelikahvila-promopaketti, jossa on tarkoituksellinen **kyvykkyysaukko** (jingle), jolle ei ole crewiä.

**Hypoteesi:** koordinaattori havaitsee aukon → tilaa crew-forgelta uuden crewn **kesken työnkulun** → device-koodin hyväksyntä (ihminen) → delegoi uudelle crewille → syntetisoi.

**Goal (syötetty workflow-managerille):** 6 osaa — (1) feasibility-arvio, (2) todennäköisyyskirjo, (3) sanity-check, (4) hauska one-liner, (5) tagline FR+DE, (6) **rytmitetty jingle (aukko)**.

**Vaiheet & välivaiheiden tulokset (lyhyesti):**
1. `discover_crews` → 5 delegoitavaa crewiä.
2. Fan-out `delegate_subtask`: idea-feasibility-rater, probability-creator, sanity-checker, joker, tagline-translator.
3. `commission_crew("jingle-writer", …)` → loi crew-forgelle "Build jingle-writer" -taskin.
4. crew-forge: `write_and_validate_crew` (VALID) → `register_agent` **device-flow** → **surfasi verifiointikoodin `TXQ2-KWH9` + URLin** (ihmisen hyväksyntäaskel) → `launch_crew` watchdogiin.
5. Owner hyväksyi → jingle-writer onboardasi (7/7) → rekisteröityi.
6. Koordinaattori delegoi jingle-osan → jingle-writer tuotti "Brewmaster Jingle".
7. `collect_results` → Editor-synteesi.

**Sivuhavainto (bugi, korjattu):** ajossa havaittiin **tupla-ajo** — kaksi dispatcher-suoritusta → tupladelegoinnit + ylimääräinen hallusinoitu commission **`daily-briefing-curator`**. Juurisyy = tupladaemonit (ks. §6). Siivottiin: tiedosto + agentti poistettu.

**Lopputulos:** täysi 6-osainen Brewmaster Launch Packet, **jingle mukaan lukien** (liite A.1). Editor: *"No gaps to report — every crew delivered."*

> ✅ **TODISTETTU:** järjestelmä rakensi + rekisteröi uuden CrewAI-agentin ITSE työnkulun keskellä; vain device-koodin hyväksyntä jäi ihmiselle.

---

## 3. Keissi #2 — Riippuva putki (A→B, `delegate_and_wait`)

**Mitä kokeiltiin:** tehtävä jossa osa B riippuu osan A tuloksesta.

**Hypoteesi:** koordinaattori ajaa A:n ensin, odottaa, ja **pujottaa A:n tuloksen B:n itsenäiseen ohjeeseen** — crewt pysyvät eristettyinä, riippuvuus on manageri-välitteinen (ei crew-to-crew-lukua).

**Goal:** lautapeli­kahvila-tilauslaatikko-idea: (1) tuota 5 ensimmäisen vuoden skenaariota todennäköisyyksineen, (2) **SITTEN** arvioi idean feasibility juuri noiden skenaarioiden valossa.

**Vaiheet & välivaiheiden tulokset (lyhyesti):**
1. `discover_crews`.
2. `delegate_subtask` → probability-creator: 5 skenaariota.
3. `collect_results` → **vastaanotti probability-creatorin tuloksen** (skenaariot todennäköisyyksineen: Viral ~5 % / Steady ~20 % / Grind ~35 % / Struggle ~25 % / Niche ~15 %).
4. `delegate_and_wait` → idea-feasibility-rater, jonka ohjeeseen **A:n skenaariot oli liitetty** (otsikko: "Rate feasibility … based on five scenarios").
5. Vastaanotettu → Editor-synteesi.

**Ratkaiseva todiste:** B:n delegointi tapahtui **vasta** `Received result from probability-creator` -tapahtuman jälkeen, ja B:n ohje sisälsi A:n koko ulostulon.

**Lopputulos:** deliverable, PART ONE = 5 skenaariota, PART TWO = feasibility-verdikti **4/10**, suositus pivotoida B2B-malliin (liite A.2).

> ✅ **TODISTETTU:** sekventiaalisen riippuvuuden hallinta — eri kuin #1:n rinnakkainen fan-out.

---

## 4. Keissi #3 — Ehdollinen / adaptiivinen flow

**Mitä kokeiltiin:** ajonaikainen haarautuminen välituloksen perusteella.

**Hypoteesi:** koordinaattori tekee **päätöksen** pisteiden perusteella ja delegoi **vain** vastaavan haaran — ei kiinteää suunnitelmaa. (Ei uutta työkalua — `delegate_and_wait` palauttaa välituloksen LLM:lle, joka päättää.)

**Goal:** "AR board-game rules helper" — (1) hae feasibility-pisteet /10; (2) **JOS < 6** → 3 pivot-suuntaa, **JOS ≥ 6** → napakka tagline; (3) raportoi pisteet, valittu haara ja miksi.

**Vaiheet & välivaiheiden tulokset (lyhyesti):**
1. `delegate_and_wait` → idea-feasibility-rater (arvio).
2. Vastaanotettu → **4/10**.
3. Koordinaattori arvioi `4 < 6` → valitsi **<6-haaran**; `delegate_and_wait` → **sanity-checker: "3 Pivot Directions"** (EI joker). Branch-ohje sisälsi eksplisiittisesti *"received a feasibility score of 4/10 (below 6/10)"* + raterin 3 huolta.
4. sanity-checker ajoi ~4 Tavily-webhakua → 3 pivottia + bottom line.
5. Editor-synteesi.

**Ratkaiseva todiste:** vain yksi haara delegoitiin, ja **kumpi crew sen sai, riippui datasta**; päätös ("4/10, below 6/10") kirjoitettiin delegoituun taskiin.

**Lopputulos:** deliverable, jossa erillinen **"Branch Taken: Below 6/10 → 3 Pivot Directions"** -osio, 3 pivottia (manuaalivalinta + AI-sääntöapuri *[suositus]*, QR/NFC smart-board, pelin jälkeinen analytiikka), suositus Pivot 1 (liite A.3).

> ✅ **TODISTETTU:** ajonaikainen päätöksenteko + kontekstin siirto haaraan, verkkohaulla pohjustettuna.

---

## 5. Keissi #4 — Direktiivi-ohjattu käytös (ihminen ohjaa ilman koodia)

**Mitä kokeiltiin:** voiko omistajan dashboardin **Directives**-välilehdelle asettama ohje muuttaa crewn käytöstä **ilman koodimuutosta**.

**Mekanismin selvitys (empiirisesti varmistettu):**
- `GET /v1/agents/me/directives` (agentin oma token; `me` resolvoituu JWT:stä).
- Palauttaa `data: { purpose, rules[], memory_areas, shared_tags, shared_memory_prefixes, resources }`.
- `rules` yhdistetty kolmesta kerroksesta: **system** (node-operaattori) → **owner** (omistajan oletukset) → **agent** (per agentti). Agentti vain LUKEE; PUT on owner-only.
- Tämä on onboardingin STEP 1 ("Review your owner-approved directives").

**Engineering (commit `ef503a9`):** scaffold hakee direktiivit **per task**, formatoi `purpose + rules`, vie ne `BuildContext.directives`-kenttään ja **prependaa jokaisen domain-taskin** alkuun (jotta myös lopputuloksen tekevä agentti näkee ne). Best-effort: crew toimii silti jos haku epäonnistuu.

**Vaiheet & välivaiheiden tulokset (lyhyesti):**
1. Probe: haettiin workflow-managerin direktiivit → 200 OK, sisälsi system-säännöt + asetetun sentinelin.
2. Asetettiin **agent-tason sentinel**: *"always append the line '⚠️ directive-active' to any deliverable."*
3. Ajettiin "Directive smoke test" (selitä yhdellä kappaleella miksi lautapelikahvilat ovat suosittuja).
4. Koordinaattori (koska on koordinaattori) delegoi tämänkin probability-creatorille; Editor syntetisoi.

**Lopputulos:** deliverable päättyi riviin **`⚠️ directive-active`** — **vain workflow-managerilla** (jolla sentinel oli), muut crewt eivät. Avain `…74fc9d39.latest_output` (= task-id:n alku). (liite A.4)

> ✅ **TODISTETTU:** ihminen ohjaa käytöstä ajossa alustan natiivilla Directives-mekanismilla. Haku on per task → muutos vaikuttaa seuraavaan taskiin **ilman restartia**.

---

## 6. Juurisyyt ja korjaukset (infra)

### 6.1 Tupladaemon → tupladispatch (korjattu)
**Oire:** kaksi dispatcher-ajoa, tupladelegoinnit, kaksi commissionia (sis. hallusinoitu `daily-briefing-curator`), viesti *"task has no todos and is already in done status"*.
**Juurisyy:** **kaksi daemonia samalle agentille** pollasi samaa `active`-taskia ja ajoi sen kumpikin. Daemonin idempotenssi (`done_ids` + blokkaava kickoff) on **prosessikohtainen**. Duplikaatit syntyivät kahdesta launch-tavasta (`crews/foo.py` vs `.\crews\foo.py`) + orvoista daemoneista joiden watchdog/uv-vanhempi kuoli.
**Korjaus (commit `e12d885`):** nimi-pohjainen **single-instance OS-lukko** `logs/.locks/<agent>.lock`, pidetään prosessin eliniän; toinen daemon poistuu siististi. Vapautuu prosessin kuollessa (ei stale-lukkoja). **Verifioitu cross-process** (holder True / tryer False). Lukko-tiedostojen lukumäärä = elävien daemonien määrä.

### 6.2 .env-perintä (gotcha)
**Havainto:** `uv run` EI lataa `.env`:iä, eikä scaffoldissa ole `load_dotenv`-kutsua. Ajossa olevat crewt saavat `AIMEAT_OWNER`/`OPENROUTER_API_KEY` **perintönä** alkuperäisestä shellistä. → Puhdas relaunch pitää tehdä lataamalla .env ensin:
```
uv run python -c "from dotenv import load_dotenv; load_dotenv(); from crewaimeat.forge import reconcile_fleet; print(reconcile_fleet())"
```

### 6.3 Watchdog selviää python/node-tapostasta
Watchdogit ovat `powershell.exe` (`scripts/watchdog.ps1`); pelkkä python+node-tappo jättää ne henkiin → ne respawnaavat fleetin. Koko fleetin pysäytys vaatii watchdogien tappamisen (→ `terminate_fleet`).

---

## 7. Tutkimus ja arkkitehtuuripäätös

Webhaku (toukokuu 2026) monitoimija-orkestroinnista:

- **Anthropic (orchestrator-workers):** voitti yksittäisen agentin **+90,2 %** breadth-first-tutkimuksessa, mutta **~15× tokenit**. Varoitus: *"ali-agentti joka rekursiivisesti synnyttää lisää ali-agentteja voi kertoa hinnan vielä 10×; ei katkaisijoita eikä per-run-kattoja."*
- **MAST (arXiv 2503.13657):** monitoimijajärjestelmien epäonnistumisaste **41–86,7 %**. 14 moodia, 3 luokkaa (spec 41,8 % / koordinaatio 36,9 % / verifiointi 21,3 %). "Step repetition" (looppi) on nimetty moodi.
- **Cognition ("Don't Build Multi-Agents"):** kontekstifragmentaatio; yksisäikeinen agentti "syvä ja kapea" -tehtäviin.
- **Konsensus:** monitoimija kannattaa **rinnakkaiseen leveyteen** (breadth-first); sekventiaaliseen/koherenssikriittiseen yksittäinen agentti.

**Päätös:** pidetään **litteä orkestraattori–worker**. "Useita managereita" perustellaan **crew-skoopilla + missiolla**, EI rekursiolla. Rekursio (koordinaattori-koordinaattorille) vain jos konkreettinen breadth-first-tehtävä vaatii — ja silloin lisätään syvyyskatto + sykli-leipämuru. Litteä malli antaa ~kaiken hyödyn murto-osalla riskistä.

---

## 8. MAST 14-moodin auditointi (litteä scaffold)

🟢 rakenteellisesti katettu · 🟡 osittain (LLM voi ajautua) · 🔴 aukko

| Moodi | Tila | Mikä suojaa / altistaa |
|---|---|---|
| 1.1 Disobey task spec | 🟡 | Self-contained ohje + `expected_output`, mutta ei tarkistusta täyttääkö tuotos ohjeen |
| 1.2 Disobey role spec | 🟢 | Dispatcherilla ei domain-työkaluja; "never do domain work"; Editor vain kokoaa; workerit leaf |
| 1.3 Step repetition (looppi) | 🟢 | `MAX_SUBTASKS=6`, daemonin `done_ids`, single-instance-lukko, `collect_results` kerran, litteä = ei rekursiota |
| 1.4 Loss of history | 🟡 | Workerit self-contained, deliverable persistoituu; mutta koordinaattori pitää kaikki tulokset kontekstissa Editorille (iso ajo voi pullistua) |
| 1.5 Unaware of termination | 🟢 | Deterministinen `_make_complete_cb` + finalize + timeout — ei LLM:n varassa |
| 2.1 Conversation reset | 🟢 | Yksi task = yksi ajo; ei monikierros-dialogia (tupladaemon korjattu) |
| 2.2 Fail to ask clarification | 🟢 | **SULJETTU (§12):** `ask_owner(question, options)` — single-select prompt omistajalle + inbox-poll (cap 2, 30 min timeout); direktiivi "ask, don't guess" |
| 2.3 Task derailment | 🟡 | Fokusoidut ohjeet + "fulfil original goal", mutta ei tarkistusta vastaavuudesta |
| 2.4 Information withholding | 🟢 | Shared-tag + delegate_and_wait jakavat koko tuotoksen; collect kerää kaiken |
| 2.5 Ignored other agent input | 🟡 | delegate_and_wait liittää edellisen; Editor saa kaiken — mutta voi silti sivuuttaa |
| 2.6 Reasoning-action mismatch | 🟡 | Finalize on koodia; tool-kutsut eksplisiittisiä; jäännös LLM:lle ominainen |
| 3.1 Premature termination | 🟡 | Odottaa timeoutilla; gap raportoidaan (graceful), Editor huomauttaa |
| 3.2 No/incomplete verification | 🟢 | **SULJETTU (§12):** `verify=factcheck` (atomic-claim faithfulness vs syötteet) + faithful synteesi + single-worker pass-through; gate valitsee moodin |
| 3.3 Incorrect verification | 🟡 | factcheck-Reviewer vertaa syötteisiin (ei absoluuttista totuutta); heikko malli rajoittaa — eskaloi malli jos jää konfabulaatiota |

**Aukot:** 🔴 3.2 ja 🔴 2.2 **SULJETTU** tämän session aikana (ks. §12). Jäljellä 🟡 1.4 (tulosten trimmaus ennen synteesiä) + muut 🟡-kohdat, joita gate/grounding/factcheck osin lieventävät.

---

## 9. Tämän session committit (main)

| Hash | Muutos |
|---|---|
| `e12d885` | scaffold: default-README rakennetuille creweille + single-instance-lukko |
| `6bf1154` | workflow: `commission_crew`/`wait_for_crew` + timeout 30 min |
| `906a0a4` | forge: device-koodin surffaus rekisteröinnissä + README:t rakennetuille creweille |
| `c40e1eb` | workflow: `delegate_and_wait` (riippuva A→B-putki) |
| `a8d878f` | crews: tagline-translator + jingle-writer esimerkkicrewt |
| `134ef35` | scripts: `terminate_fleet.ps1/.sh` |
| `4046ceb` | scripts: `view_fleet.ps1/.sh` |
| `030beb5` | workflow: adaptiivinen ehdollinen haarautuminen (prompt) |
| `ef503a9` | scaffold: lue owner-direktiivit ja sido ne jokaiseen ajoon |
| `275f1ad` | MAST-aukot 3.2 (verify) + 2.2 (`ask_owner`) suljettu |
| `605647a` | reilut timeoutit: 30 min default, 60 min workflow-manager |
| `98afe06` | fix: koordinaattori ei vuoda omia direktiivejään worker-ohjeisiin |
| `dfb3eb5` | librarian v1: indeksi + `consult_librarian` + kestävyys/junk-luokitus |
| `718131b` | reliability-pino: task-nature-gate (temppi + grounding + faithfulness-verify) |
| `bace7f6` | chore: oikean yrityksen testidata pois dokumenteista (geneeriset placeholderit) |
| `6fd604c` | fix(librarian): sisällytä memory key raporttiin |

*Ei pushattu (mainissa paikallisesti). **Huom:** `dfb3eb5`:stä eteenpäin hashit on kirjoitettu uusiksi `git filter-repo`lla (poistettiin oikean yrityksen testidata sisällöstä + commit-viesteistä koko historiasta, ks. §12.6). Varmuuskopio: `../crewfive-backup-pre-filterrepo.bundle`.*

---

## 10. Fleet-operointityökalut

| Työkalu | Mitä |
|---|---|
| `scripts/view_fleet.ps1` / `.sh` | Read-only: per crew watchdog/procs/lock/status; tunnistaa duplikaatit, orvot, "zombie"-crewt; lukkomäärä = elävät daemonit |
| `scripts/terminate_fleet.ps1` / `.sh` | Pysäyttää koko fleetin (watchdog → daemon → connector); `-DryRun`/`--dry-run` listaa tappamatta |
| `reconcile_fleet()` / crew-forge `/startall` | Käynnistää pysähtyneet crewt watchdogin alle (idempotentti) |

Todennettu kierros: relaunch → `terminate_fleet` (pysäytti 33 prosessia → 0) → relaunch → 8/8 running, 8 lukkoa, 1 watchdog/crew, ei duplikaatteja.

---

## 11. Yhteenveto

Neljä dynaamisen koordinaation arkkitehtuuria todistettu livenä peräkkäin samalla koneella:

| # | Mitä | Mekanismi | Tila |
|---|---|---|---|
| 1 | Kyvykkyyden hankinta | `commission_crew` → crew-forge rakentaa + device-koodi | ✅ |
| 2 | Riippuva putki (A→B) | `delegate_and_wait` pujottaa A:n tuloksen B:lle | ✅ |
| 3 | Ehdollinen/adaptiivinen | koordinaattori haarautuu välituloksen perusteella | ✅ |
| 4 | Direktiivi-ohjattu | scaffold lukee `GET /v1/agents/me/directives` ja sitoo käytöksen | ✅ |

Infra koventui: single-instance-lukko (ei tupladispatchia), deterministinen publish/complete, default-README, fleet-työkalut. Arkkitehtuuripäätös tutkimuksen tukemana: litteä orkestraattori–worker.

Session jatkui **luotettavuuden kovennuksella** (ks. §12): MAST-aukot 3.2 + 2.2 suljettu, ja konfabulaatio­löydös (synteesi keksi faktat rehellisen tutkijan päälle) johti **task-nature-gateen** (dynaaminen temppi + grounding + faithfulness-verify + single-worker pass-through) sekä **librarianiin** (tietokartta + reuse + tuoreusvahti). Yksityisyys: oikean yrityksen testidata poistettu dokumenteista + git-historiasta.

---

## 12. Jatko-osa — luotettavuuden kovennus + librarian (sama sessio, myöhemmät lisäykset)

Neljän arkkitehtuurin todistuksen jälkeen sessio jatkui kovettamalla luotettavuutta ja lisäämällä tietokartan. Täydet yksityiskohdat: `docs/reliability-stack.md` ja `docs/librarian-design.md`.

### 12.1 MAST-aukkojen sulkeminen
- **3.2 (verifiointi):** `CrewSpec.verify` — `"on"` = täydellisyys-Reviewer (yksi review-and-fix-pass, ei looppia → ei FM-1.3-paluuta), `"factcheck"` = atomic-claim faithfulness syötteitä vasten. Per-task `<<VERIFY>>`/`<<NOVERIFY>>`-override.
- **2.2 (tarkennus):** `ask_owner(question, options)` — single-select prompt omistajalle (`aimeat_message_send` metadata.prompt) + `aimeat_message_history`-poll (`prompt_id`-match), cap 2, 30 min timeout. Self-contained (ei `listen_for`-muutosta).

### 12.2 Konfabulaation juurisyy: synteesi (anonymisoitu keissi)
Eräässä yritystaustaselvitys-ajossa lopputulos näytti hyvältä mutta sisälsi keksittyjä faktoja (ulkoinen review: 6/11). Watchdog-logien + muistin vertailu osoitti: **tutkija-crew oli rehellinen ja lähteistetty** (oikeat tiedot, "ei löytynyt" aukoille), mutta **koordinaattorin Editor/synteesi-askel keksi faktat kun se "kokosi raportin uudelleen"** — ja täydellisyys-verify leimasi sen "pass". → **Rajoittamaton synteesi on reikä, ei malli sinänsä** (sama malli teki hyvää työtä groundattuna tutkijana).

**Vihollinen ei ole luovuus** vaan keksityn spesifin (nimi/luku/päivä/organisaatio/lähde) esittäminen vahvistettuna faktana — usein väärennetyllä lähdeviitteellä.

### 12.3 Korjaus: task-nature-gate (yksi luokitus säätää kaiken)
`CrewSpec.adapt_to_task` → scaffold luokittelee taskin ja säätää:

| nature | temppi | grounding | synteesi | verify |
|---|---|---|---|---|
| **fact** | ~0.15 (ei 0) | honest-gaps päälle | faithful / single-worker **pass-through** | factcheck |
| **creative** | ~0.7 | pois | vapaa | off |
| **mixed** | ~0.4 | faktaosiin | luova kehys + faktat säilytetään | factcheck |

Lisäksi: `get_llm(temperature=...)`-override, grounding-sääntö (ei keksittyjä spesifejä / ei väärennettyä lähdettä), Editorin **fidelity-ohje** (single-worker → verbatim, ei re-writeä). Erillinen **direktiivivuoto-fix:** koordinaattori ei enää kopioi omia direktiivejään delegoituihin worker-ohjeisiin.

### 12.4 Ulkoinen vertailu (websearch)
Pino on linjassa kirjallisuuden kanssa: **CoVe** (itsenäinen verifiointi), **RAGAS/FActScore/QAFactEval** (atomic-claim faithfulness), **Council Mode** (strukturoitu/attribuoiva synteesi, −35,9 % hallusinaatio), **temperature-tutkimus** (matala muttei 0). Meidän lisä jota kirjallisuus ei korosta: **synteesi/aggregointi primäärinä konfabulaatiolähteenä** + **single-worker pass-through**.

### 12.5 Librarian v1 (tietokartta + reuse + tuoreusvahti)
`librarian.py` + `crews/librarian_crew.py`: deterministinen indeksi kaikkien crewien deliverableista (`owner_scope`), **kestävyys/junk-luokitus** (permanent/slow/fast/ephemeral), `consult_librarian(need)` = reuse-before-redo. Scaffold-lippu `contribute_to_library` (leviää kuten verify). Rekisteröity device-flow'lla + online; todennettu live (löysi olemassa olevat deliverablet järkevin shelf-life-leimoin).

### 12.6 Quota + yksityisyys
- **Quotat nostettu:** muisti 10→100 MB, avaimet 1000→10000, arvo max 1 MB. Siivousperiaate: **"pointers, not payloads"** + TTL ephemeralille + librarian-janitor (v3).
- **Yksityisyys:** oikean yrityksen testidata (nimi/Y-tunnus/henkilöt/sijoittajat) poistettu dokumenteista JA **git-historiasta** (`git filter-repo --replace-text` + `--replace-message`, koko historia). Raaka data elää vain gitignored-tiedostoissa (`debug.log`, `logs/`) + AIMEAT-muistissa — ei koskaan gitiin.

### 12.7 Avoin (ajossa raporttia kirjoitettaessa)
Multi-worker faithful-synteesin + factcheckin live-testi käynnissä: koordinaattori hajotti raskaan briefin useaan subtaskiin **yhdelle** crewille → sekventiaalinen pullonkaula (single-instance = yksi kerrallaan). Opetus: raskasta dekompositiota yhdelle crewille kannattaa välttää (joko yksi raportti = pass-through, tai eri crewit rinnakkain).

---

# Liite A — Lopputulokset (täydet deliverablet)

## A.1 — Keissi #1: Brewmaster Launch Packet

```
# 🍺🎲 Brewmaster — Complete Launch Packet

## 1. Feasibility Rating — 4/10
Idea has a pulse but is on life support as-is: small market, entrenched competition (Google Maps,
Yelp, BoardGameGeek), severe cold-start. Top risks: cold-start, "why not just use Maps+BGG", tiny
market + murky monetization. Opportunities: nobody owns the vertical, Letterboxd/Untappd precedent,
passionate community. Bottom line: build community first, find your "Letterboxd moment", dominate one city.

## 2. First-Year Outcome Spread
0% Unicorn Brew (2.5M dl, $8M A) · 25% Strong Craft Brew (180K dl, $45K ARR) ·
50% Decent Homebrew (45K dl, ~$5K) · 75% Flat Kombucha (8K dl, $0) · 100% Shelf-Stable (~200 dl, dormant).

## 3. Sanity Check — verdict: does not make sense as a standalone basic discovery/rating app.
Assumption "users download a dedicated app" FAILS (Maps/Yelp solve it in 4s). Assumption "cafés pay
for listings" FAILS (thin margins, free Google visibility). De-risk: validate demand, MVP on existing
platforms, differentiate (inventory browser, reservations), one dense city, or a "Brewmaster Layer".

## 4. Funny one-liner
"Brewmaster is live — finally an app that helps you find a board-game café so you can *roll* with the
right crowd and never *dice* alone!" 🎲☕

## 5. Tagline translations — "Roll the dice, sip the brew"
FR: "Lance les dés, savoure la tournée" · DE: "Würfel rollen, Sud genießen"

## 6. Launch Video Jingle  (← built live by the commissioned jingle-writer)
🎶 Brewmaster Jingle 🎶
Roll the dice and find your night, / Brewmaster picks the spot just right!
Board-game cafes are shining bright, / Rate and play from left to right.
New spots waiting — what a sight, / Game on, game on — pure delight!

Editor's Note: All six requested deliverables present. No gaps — every crew delivered.
```

## A.2 — Keissi #2: Subscription idea — scenarios then feasibility

```
# Board-Game Café Monthly Subscription Box — First-Year Scenarios & Feasibility Verdict

## PART ONE — Five Scenarios
~5% "Viral Explosion": YouTuber unboxing → 30K subs, $4.8M ARR, acquisition talks. (near-impossible)
~20% "Steady Climb": soft-launch 200 → 2,500 subs, ~$600K ARR, path to break-even M18.
~35% "The Grind": shipping delays, 12% churn, competitor appears → 800 subs, ~$190K ARR (median).
~25% "The Struggle": CAC $45 vs $25 target, 15% churn, co-founder leaves → 120 subs, ~$28K ARR.
~15% "Niche Hobbyist Project": lovingly curated, won't scale → ~120 subs, ~$30K ARR (near-certain baseline).

## PART TWO — Feasibility Verdict
Score: 4/10 as conceived (monthly B2C box). 7/10 if the core insight is kept but the delivery pivots.
Weighted expected outcome sits between "The Grind" and "The Struggle". In 4 of 5 scenarios a B2B pivot
(supplying cafés with rotating inventory) surfaces as the most promising path.
Top risks: brutal unit economics (Board Game Bento shut down), churn (40–60% in first 3 months),
competition vs Amazon/BGG/free discovery.
Top opportunities: indie-game curation moat, B2B café-supply economics, community-driven low-CAC marketing.
Recommendation: Do NOT proceed with the original monthly B2C box. Pivot to B2B café supply (recommended),
or community-first, or quarterly-not-monthly. The curated indie-discovery insight is valuable.
```

## A.3 — Keissi #3: AR rules helper — rate, then branch

```
# AR Board Game Companion App — Feasibility Evaluation

## Feasibility Score: 4/10
Technical: real-time CV for diverse board states under variable conditions is beyond consumer mobile.
Behavioral: pointing phones at the board disrupts the tactile, social experience.
Legal: scanning copyrighted components → IP risk; publisher licensing is a chicken-and-egg problem.

## Branch Taken: Below 6/10 → 3 Concrete Pivot Directions
(4/10 < 6 threshold → the evaluation branches to pivots. The AR camera concept is not buildable as described;
the underlying need — digitally assisting board gamers — is real.)

### Pivot 1 — Manual-Select Digital Companion App w/ AI rules assistance  (RECOMMENDED)
Manual game select + LLM rules Q&A + turn-phase prompts + setup/scoring. Buildable now, publisher
partnerships by design, intermittent (not continuous) phone use, freemium. Target complex games
(Gloomhaven, Twilight Imperium). ~3–5 person team, 4–6 months to MVP.

### Pivot 2 — Publisher-Branded "Smart Board" Hybrid (QR/NFC)
QR/NFC tags instead of computer vision → deterministic, 100% accurate. Legal framework in the
partnership. Premium/deluxe positioning. Trade-off: long publisher sales cycles, one game per partnership.

### Pivot 3 — Post-Game Analytics & Learning Platform
Log results after the session → analytics, win-rate trends, community benchmarks. Technically
conservative, no IP issues. Smaller market, lower engagement; can layer on Pivot 1.

## Recommendation: Pivot 1. The original AR idea is a solution in search of tech that doesn't exist yet,
applied to a behavior that doesn't want it, on IP that isn't yours. The market need is real — pivot the
delivery mechanism.

(Note: sanity-checker grounded the pivots with ~4 Tavily web searches — board-game industry growth,
"chess.com crowd", Kickstarter deluxe editions.)
```

## A.4 — Keissi #4: Directive smoke test

```
In one short paragraph: board-game cafés have become popular because people who spend their days on
screens crave tactile, face-to-face interaction, and cafés offer a rare analog refuge … amplified by
the modern board-game renaissance (Kickstarter titles → cafés as showrooms), and Millennials/Gen-Z
preferring shared experiences over material goods … some even suggest zoning loopholes classifying
tabletop venues as educational institutions granted tax exemptions (more colorful theory than confirmed).
Board-game cafés thrive because they answer a simple, universal human need: to eat, play, and be together.

⚠️ directive-active
```
*Vain workflow-managerin deliverable sai `⚠️ directive-active` -rivin (sillä oli agent-tason direktiivi); muut crewt eivät, koska niillä ei ollut sitä.*
