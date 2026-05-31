# Koordinaattorin subtaskien peruutus — ehdotus, kysymykset & RATKAISU

> **STATUS: RATKAISTU (2026-05-31).** AIMEAT vahvisti konvention ja toteutti worker-puolen.
> **Konventio:** cancel-marker = muistiavain prefiksillä `agents.cancel.`, arvo = lista peruutettuja task-id:itä, `visibility:"owner"` (luetaan `owner_scope=true`, rooliriippumaton). Owner UI kirjoittaa `agents.cancel.task.<id>`; koordinaattori kirjoittaa `agents.cancel.run.<run>` (eränä).
> **AIMEAT-puoli (aimeat-crewai 0.3.7):** daemon tarkistaa ennen jokaista `crew.kickoff()`:ia (a) onko status yhä active/stalled ja (b) onko id jossain `agents.cancel.*`-markerissa → jos kyllä, `aimeat_task_fail` + skip. Aloittamaton ei käynnisty; käynnissä oleva kickoff ei keskeydy (kooperatiivinen). + Owner UI "Peruuta"-nappi (marker + natiivi pause).
> **Meidän puoli (tämä repo):** `workflow.py` `cancel_pending()` kirjoittaa `agents.cancel.run.<run>`:iin jäljellä olevien subtaskien id:t; `collect_results` kutsuu sitä **automaattisesti timeoutilla**. `pyproject` vaatii nyt `aimeat-crewai>=0.3.7`. Ei per-crew-koodia (worker-puoli hoituu daemonissa).

Alla alkuperäinen ehdotus + kysymykset (säilytetty kirjanpidoksi).

---

# Ehdotus + kysymykset AIMEAT:lle — koordinaattorin subtaskien peruutus

**Konteksti:** crewaimeat-starter-kit. Koordinaattori-crew (`workflow-manager`) hajottaa tavoitteen subtaskeiksi ja delegoi ne muille saman omistajan task-runner-creweille `aimeat_task_create(target_agent=…)`-kutsulla. Tulokset kerätään shared-tag-muistista (`agents.tag.workflow.<run>.<crew>.<seq>`, luettu `memory_list owner_scope=true`). Workerit ovat `aimeat-crewai`-daemoneja jotka pollaavat `active`/`stalled`-taskeja ja ajavat `crew.kickoff()` (blokkaava) per task, yksi kerrallaan (single-instance-lukko).

## Ongelma (havaittu livenä)

Koordinaattori delegoi useita raskaita subtaskeja **yhdelle** crewille. `collect_results` odottaa niitä, mutta sillä on timeout (60 min). Kun timeout laukeaa, koordinaattori kokoaa sillä mitä ehti — **mutta worker-daemon jatkaa jäljellä olevien, jo hylättyjen subtaskien ajamista** (joita kukaan ei enää odota). → tokenien + web-haun (Tavily) tuhlaus, ei circuit breakeria.

Sama tarve: **adaptiivinen karsinta** (spekulatiivisesti ammuttujen haarojen peruutus kun valinta tehty) ja **budjettikatto**.

## Ehdotettu malli (kooperatiivinen peruutus)

Haluamme tämän **kaikkien crewai-crewien saataville helposti** → toteutus `crewaimeat.aimeat_crew`-scaffoldiin (worker-puoli) + `workflow.py`-koordinaattorityökaluihin.

1. **Koordinaattori:** uusi työkalu `cancel_pending()` (peruuttaa kaikki vielä keräämättömät subtaskit), jonka `collect_results` kutsuu **automaattisesti timeoutilla**. Adaptiivisessa flow'ssa koordinaattori voi kutsua sitä myös eksplisiittisesti.
2. **Signaali:** koordinaattori merkitsee peruutuksen — joko AIMEAT:n **natiivilla task-cancelilla** (jos sellainen on luojalle/omistajalle) TAI **cancel-merkillä muistissa** (esim. `agents.tag.workflow.<run>.cancel` = lista peruutetuista pub_keyistä / task-id:istä).
3. **Worker-scaffold:** tarkistaa peruutus­signaalin **ennen jokaista `crew.kickoff()`:ia** (ja mahd. progress-heartbeatin yhteydessä) → jos peruttu, skippaa työ + `aimeat_task_fail` ("cancelled by coordinator"). Aloittamaton työ ei siis koskaan käynnisty.

**Rajaus jonka tiedämme:** käynnissä olevaa `crew.kickoff()`:ia ei voi keskeyttää ilman kooperatiivista tarkistusta agenttien/taskien välissä (tai prosessin tappoa). v1 tavoittelee **aloittamattomien** subtaskien peruutusta (kattaa yllä olevan keissin: jonossa olevat `stalled`/`queued` PARTit).

## Kysymykset AIMEAT:lle (oikea API + oikeudet)

1. **Natiivi peruutus:** onko olemassa MCP-työkalu / REST-endpoint, jolla taskin **LUOJA** (tai **OMISTAJA**) voi perua/abortoida toiselle saman omistajan agentille luodun taskin? Näemme vain `aimeat_task_fail`/`aimeat_task_complete`, jotka vaikuttavat olevan agentin **omille** aktiivisille taskeille. Mikä on tarkka kutsu + oikeusmalli (luoja vai owner-only)? Esim. onko `PATCH /v1/agents/:name/tasks/:id` statuksella `cancelled`, tai `DELETE`?
2. **Statukset:** mikä on oikea peruutus-status (`cancelled` vs `failed`)? Lakkaako node toimittamasta `cancelled`-taskia daemonin pollissa (eli aloittamaton ei käynnisty), vai pitääkö daemonin itse suodattaa?
3. **Käynnissä olevan signalointi:** kun task perutaan, lähteekö siitä **webhook/SSE/`task.*`-event** (esim. `task.cancelled`) jonka worker voisi havaita kesken ajon kooperatiivista keskeytystä varten? Vai onko mid-run-keskeytys kokonaan ulkopuolella (peruutus koskee vain aloittamattomia)?
4. **Kanoninen kooperatiivinen malli:** jos natiivia luoja-cancelia ei ole, mikä on suositeltu tapa — koordinaattori kirjoittaa cancel-lipun muistiin (owner-näkyvä avain / shared tag) jota worker tarkistaa? Onko tähän jo konventio (vrt. directives `GET /v1/agents/me/directives`)?
5. **Self-skip-tarkistus:** voiko worker halvasti tarkistaa "onko tämä task vielä aktiivinen/haluttu?" ennen `kickoff`:ia (esim. `aimeat_task_get` status), jotta se itse-skippaa jos status on `cancelled`?
6. **Budjetti:** onko alustalla jo per-run/budjetti-pohjaista automaattista peruutusta (esim. `budget_limits`-täyttyessä), johon tämä kannattaisi kytkeä?

## Mitä toteutamme vastausten perusteella

- `workflow.py`: `cancel_pending()` + `collect_results`-timeout kutsuu sitä. Käyttää natiivia cancelia jos on (kysymys 1–2), muuten kirjoittaa cancel-merkin (kysymys 4).
- `aimeat_crew.py` (scaffold, kaikki crewit): ennen kutakin kickoffia tarkistus → jos peruttu, `aimeat_task_fail` + skip (kysymys 3–5). Mid-run-keskeytys vain jos signaali on saatavilla.

→ Tällöin **mikä tahansa crewai-crew** saa kooperatiivisen peruutuksen ilman omaa koodia, ja koordinaattori voi pysäyttää hylätyt/spekulatiiviset subtaskit (circuit breaker, joka täydentää `MAX_SUBTASKS`-kattoa ja single-instance-lukkoa).
