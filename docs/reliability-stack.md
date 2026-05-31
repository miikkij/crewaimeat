# Luotettavuuspino — task-nature-gate konfabulaatiota vastaan

**Status:** suunnitelma (meidän idea) + ulkoinen vertailu kesken · **Päivä:** 2026-05-31

## Mistä tämä lähti (***REMOVED***-keissi)

workflow-manager commissionoi `finnish-corporate-researcher`n tekemään yritystaustaselvityksen. Lopputulos näytti hyvältä mutta sisälsi vaarallisia hallusinaatioita (ulkoinen Claude-review: 6/11 faktaa keksitty). Watchdog-logien + muistin vertailu paljasti **mistä konfabulaatio tuli:**

| Termi | Tutkijan oma raportti | Watchdog-logi | Lopullinen deliverable (Editor) |
|---|---|---|---|
| ***REMOVED*** (oikea sijoittaja) | **6×** ✅ | – | korvattu |
| ***REMOVED*** (oikea sijoittaja) | **5×** ✅ | – | korvattu |
| ***REMOVED*** (keksitty) | **0** | **0** | **läsnä** ❌ |
| ***REMOVED***/***REMOVED*** (keksitty) | 0 | (haku­kohina) | **läsnä** ❌ |
| 3 rahoituskierrosta (keksitty) | 0 | – | **läsnä** ❌ |

**Johtopäätös:** tutkija oli rehellinen ja lähteistetty (löysi oikeat sijoittajat, merkitsi aukot "ei julkista tietoa löytynyt"). **workflow-managerin Editor/synteesi-askel (owl-alpha) keksi hallusinaatiot kun se "kokosi raportin uudelleen"** — ja verify (vain täydellisyystarkistus) leimasi sen "pass".

→ **Rajoittamaton synteesiaskel on reikä, ei malli sinänsä.** Sama malli teki hyvää lähteistettyä työtä tutkijana kun se oli groundattu hakuun + ohjeistettu honest gaps -periaatteella.

## Vihollinen ei ole luovuus

Editor ei tehnyt syntiä olemalla luova. Synti oli **keksityn spesifin (nimi/luku/päivä/organisaatio/lähde) esittäminen vahvistettuna faktana** — usein **väärennetyllä lähdeviitteellä** ("***REMOVED*** | Crunchbase / Kauppalehti").

> **Periaate:** Luovissa tehtävissä keksi vapaasti. Mutta älä KOSKAAN esitä keksittyä spesifiä vahvistettuna faktana, äläkä liitä keksittyä lähdeviitettä. Fakta-tehtävissä: lähteistä tai "ei löytynyt".

## Ydin: task-nature-gate (yksi halpa luokitus säätää kaiken)

Taskin alussa scaffold luokittelee `{nature, strictness}` ja se ohjaa koko pinon:

| nature | temppi | grounding | synteesi | verify |
|---|---|---|---|---|
| **fact** | matala ~0.15 | honest-gaps päälle | **faithful** (älä lisää faktoja syötteiden ulkopuolelta) | factcheck / faithfulness |
| **creative** | korkea ~0.7 | pois | **vapaa** (saa keksiä) | completeness / off |
| **mixed** | keski | vain faktaosiin | luova kehys + faktat säilytetään sanatarkasti | per-osa |

Sama muoto kuin librarianin durability-luokitus ja verify — johdonmukaista.

## Komponentit

1. **Task-nature-gate** — scaffold, per task (leviää kuten `verify`/direktiivit). CrewSpec-oletus, koordinaattori voi yliajaa; välittää nature-vihjeen delegoituihin subtaskeihin → workerit säätävät omansa.
2. **Dynaaminen temppi** — `get_llm(temperature=...)` (nyt yksi globaali 0.3).
3. **Grounding-sääntö** — fakta-moodiin + owner-defaultteihin (leviää natiivisti kaikille agenteille).
4. **Synteesi:** single-worker → **pass-through** (ei re-synteesiä, ei riskiä); multi-worker → faithful (fact) / vapaa (creative).
5. **verify-moodit:** `completeness` (nykyinen) | `faithfulness` (vertaa synteesiä worker-syötteisiin: "onko väitettä jota ei ole syötteissä?" — tämän heikkokin malli osaa).
6. **Malli:** owl-alpha ok *groundattuna*; eskaloi (esim. grok-4-fast) vain jos faithfulness-verify yhä nappaa konfabulaatiota.

## Miksi tämä korjaa ***REMOVED***n

- Single-worker pass-through olisi palauttanut tutkijan **oikean** raportin (***REMOVED***/***REMOVED***) ilman Editorin keksintöjä.
- Faithfulness-verify olisi napannut ***REMOVED***in heti ("tätä ei ole tutkijan syötteessä").
- Grounding-sääntö olisi estänyt arvio-haarukat + väärennetyt lähdeviitteet.
- Creative-tehtävät (esim. jingle) eivät kärsi — gate päästää ne luoviksi.

## Ulkoiset lähestymistavat (websearch 2026-05-31)

- **Chain-of-Verification (CoVe)** ([arXiv 2309.11495](https://arxiv.org/abs/2309.11495)): draft → suunnittele verifiointikysymykset → vastaa niihin **itsenäisesti** (ei draftin vinouttamana) → lopullinen verifioitu vastaus. = meidän verify, tarkennus: **verifioi tuoreessa passissa**, ei draftin kontekstissa. Rajoite: nojaa samaan malliin löytämään omat virheensä (= meidän "heikko malli ei tiedä totuutta paremmin").
- **Faithfulness-metriikat — RAGAS / FActScore / QAFactEval** ([Ragas docs](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/), [SAFE/FActScore primer](https://aman.ai/primers/ai/factuality-in-LLMs/)): pilko vastaus **atomisiin väitteisiin**, tarkista JOKAINEN lähdettä vasten; faithfulness = tuetut/kaikki. = meidän verify=faithfulness, tarkennus: **atomic-claim-dekompositio** (tiukempi kuin "onko väitettä jota ei ole syötteissä"). QAFactEval erityisesti summaroinnille.
- **Council Mode — multi-agent consensus** ([arXiv 2604.02923](https://arxiv.org/abs/2604.02923)): synteesimalli jäsentää consensus / erimielisyydet / uniikit / analyysi — "säilyttää vähemmistönäkemykset, suodattaa yksittäiset hallusinaatiot", **-35,9 % hallusinaatio**. → tukee meidän **strukturoitua/attribuoivaa synteesiä** (ei vapaata re-writeä).
- **Temperature** ([172B-token study, arXiv 2603.08274](https://arxiv.org/abs/2603.08274)): matala temppi yleensä vähentää hallusinaatiota MUTTA **temp 0 ei ole paras** — se voi LISÄTÄ fabrikaatiota poistamalla mallin "pakotien" matalan relevanssin fraaseista. → fakta-temppi **~0.15–0.2, EI 0**; testaa empiirisesti.

## Miten meidän idea vertautuu

| Meidän komponentti | Vastaava tutkimus | Status |
|---|---|---|
| verify=faithfulness (synteesi vs syötteet) | RAGAS / QAFactEval faithfulness | ✅ validoitu — **adoptoi atomic-claim-dekompositio + CoVe-tyylinen itsenäinen verifiointi** |
| faithful / strukturoitu synteesi | Council Mode | ✅ validoitu |
| task-nature-gate → dynaaminen temppi | temperature-tutkimus | ✅ tuettu — **mutta fakta-temppi ~0.15, EI 0** |
| grounding-sääntö (honest gaps) | RAG groundedness | ✅ vakiokäytäntö |

**Meidän lisä jota kirjallisuus ei korosta:**
1. **Synteesi/aggregointi PRIMÄÄRINÄ konfabulaatiolähteenä** — useimmat työt keskittyvät generaattoriin; meidän ***REMOVED***-keissi osoitti että *rehellisen tutkijan* päälle ajettu synteesi keksi faktat. Aggregaattorin oma hallusinaatio on aliarvioitu reikä.
2. **Single-worker pass-through** — kirjallisuus synretisoi aina; me skippaamme synteesin kun on yksi worker → ei synteesihallusinaatiota lainkaan. Halvin ja varmin korjaus tähän keissiin.
