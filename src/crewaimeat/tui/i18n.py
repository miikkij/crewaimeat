"""Minimal i18n for the fleet TUI — English + Finnish. `t(key, lang)` returns the string for the
chosen language, falling back to English then the raw key. Only UI CHROME is translated; agent
names, statuses and log text are data and stay as-is. Default language: $AIMEAT_TUI_LANG (en|fi),
else en. Toggle live with the `f` key.
"""

from __future__ import annotations

import os

LANGS = ("en", "fi")

STRINGS: dict[str, dict[str, str]] = {
    "health.title": {"en": "SYSTEM STATUS", "fi": "JÄRJESTELMÄN TILA"},
    "health.open": {"en": "Open status and recovery instructions (h)", "fi": "Avaa tila ja korjausohjeet (h)"},
    "health.beacon.alert": {"en": "ALERT", "fi": "HÄLYTYS"},
    "health.beacon.warning": {"en": "NOTICE", "fi": "HUOMIO"},
    "health.beacon.checking": {"en": "CHECKING", "fi": "TARKISTUS"},
    "health.beacon.ok": {"en": "OK", "fi": "OK"},
    "health.refresh": {"en": "Refresh (r)", "fi": "Päivitä (r)"},
    "health.hint": {"en": "Esc: back  |  f: FI/EN  |  q: quit", "fi": "Esc: takaisin  |  f: FI/EN  |  q: lopeta"},
    "health.scope": {
        "en": "Local fleet, connection and installed versions. Findings update automatically. Instructions do not run commands. This does not test model keys or individual tasks.",
        "fi": "Paikalliset agentit, yhteys ja asennetut versiot. Havainnot päivittyvät automaattisesti. Ohjeet eivät suorita komentoja. Malliavaimia tai yksittäisiä tehtäviä ei testata tässä.",
    },
    "health.pending": {"en": "Still checking: {sources}", "fi": "Tarkistus kesken: {sources}"},
    "health.checking": {"en": "Waiting for the first results.", "fi": "Odotetaan ensimmäisiä tuloksia."},
    "health.clear": {
        "en": "No problems found in the completed checks.",
        "fi": "Valmiissa tarkistuksissa ei havaittu ongelmia.",
    },
    "health.source.local": {"en": "local fleet", "fi": "paikalliset agentit"},
    "health.source.node": {"en": "server connection", "fi": "palvelinyhteys"},
    "health.source.versions": {"en": "versions", "fi": "versiot"},
    "health.source.detail": {"en": "selected agent details", "fi": "valitun agentin lisätiedot"},
    "health.read.title": {"en": "A check failed", "fi": "Tarkistus epäonnistui"},
    "health.read.help": {
        "en": "The last displayed values may be out of date. Read the error above, check the connection and logs, then press Refresh. Run uv run crewaimeat doctor --live for a fuller diagnosis.",
        "fi": "Viimeksi näytetyt tiedot voivat olla vanhentuneita. Lue yllä oleva virhe, tarkista yhteys ja lokit ja paina Päivitä. Lisäselvitys: uv run crewaimeat doctor --live.",
    },
    "health.serve.title": {"en": "Connection service information is missing", "fi": "Yhteyspalvelun tiedot puuttuvat"},
    "health.serve.help": {
        "en": "Start from this checkout. Check uv run crewaimeat doctor. To bring up the fleet: PowerShell ./scripts/start_fleet.ps1; macOS/Linux ./scripts/start_fleet.sh. Complete account approval if requested.",
        "fi": "Aja komennot tämän repon hakemistosta. Tarkista uv run crewaimeat doctor. Agenttien käynnistys: PowerShell ./scripts/start_fleet.ps1; macOS/Linux ./scripts/start_fleet.sh. Hyväksy tunnukset tarvittaessa.",
    },
    "health.serve_process.title": {
        "en": "Connection service process was not found",
        "fi": "Yhteyspalvelun prosessia ei löytynyt",
    },
    "health.serve_process.help": {
        "en": "A saved PID alone does not prove the service is running. Refresh and inspect logs/serve*. If it is stopped, use this checkout's start_fleet script. A failed process scan can also cause this finding.",
        "fi": "Tallennettu PID ei yksin todista palvelun olevan käynnissä. Päivitä ja tarkista logs/serve*-lokit. Jos palvelu on pysähtynyt, käytä tämän repon start_fleet-skriptiä. Myös prosessilistan lukuvika voi aiheuttaa havainnon.",
    },
    "health.connectors.title": {
        "en": "Several connection service processes detected",
        "fi": "Useita yhteyspalvelun prosesseja havaittu",
    },
    "health.connectors.help": {
        "en": "Confirm which processes belong to this checkout. Return with Esc; d opens the confirmation to reap stray serve daemons. Do not stop another checkout's service.",
        "fi": "Varmista ensin, mitkä prosessit kuuluvat tähän repoon. Palaa Escillä; d avaa ylimääräisten yhteyspalvelujen siivouksen vahvistuksen. Älä pysäytä toisen repon palvelua.",
    },
    "health.roster.title": {"en": "No local agents found", "fi": "Paikallisia agentteja ei löytynyt"},
    "health.roster.help": {
        "en": "Open the TUI from the project root and check the crews/ directory. Run uv run crewaimeat doctor to inspect the local roster.",
        "fi": "Avaa TUI projektin juurihakemistosta ja tarkista crews/-hakemisto. Tarkista paikalliset agentit komennolla uv run crewaimeat doctor.",
    },
    "health.duplicate.title": {"en": "An agent has competing runtimes", "fi": "Agentilla on kilpailevia ajajia"},
    "health.duplicate.help": {
        "en": "Inspect the listed agent's logs and run_mode on the server. Use one launch path: spawn, host or legacy watchdog. Stop the duplicate supervisor before restarting, so it cannot respawn.",
        "fi": "Tarkista nimetyn agentin lokit ja palvelimen run_mode. Käytä yhtä ajotapaa: spawn, host tai vanha watchdog. Pysäytä ylimääräinen valvoja ennen uudelleenkäynnistystä, ettei se käynnistä kopiota uudelleen.",
    },
    "health.zombie.title": {
        "en": "A running agent has no local crew file",
        "fi": "Käynnissä olevan agentin crew-tiedosto puuttuu",
    },
    "health.zombie.help": {
        "en": "Check the process command line and its checkout. Restore its crew file if it should run, or stop its supervisor and process if it was retired. Do not delete memory or registrations just to clear this warning.",
        "fi": "Tarkista prosessin komentorivi ja repo. Palauta crew-tiedosto, jos agentin kuuluu toimia, tai pysäytä sen valvoja ja prosessi, jos agentti on poistettu käytöstä. Älä poista muistia tai tunnuksia vain varoituksen poistamiseksi.",
    },
    "health.stale.title": {
        "en": "The server has not heard from a running agent",
        "fi": "Palvelin ei ole kuullut käynnissä olevasta agentista",
    },
    "health.stale.help": {
        "en": "Return with Esc, select the agent and open Logs (l). Check the connection and authentication errors. Run uv run crewaimeat doctor --live; reconnect only if the token was rejected.",
        "fi": "Palaa Escillä, valitse agentti ja avaa Lokit (l). Tarkista yhteys- ja tunnistautumisvirheet. Aja uv run crewaimeat doctor --live; liitä tunnus uudelleen vain, jos se on hylätty.",
    },
    "health.orphan.title": {"en": "An agent process has no watchdog", "fi": "Agenttiprosessilta puuttuu valvoja"},
    "health.orphan.help": {
        "en": "A foreground development run can be intentional. If this agent should recover automatically, launch it through the intended fleet supervisor instead of adding a second process.",
        "fi": "Käsin käynnistetty kehitysajo voi olla tarkoituksellinen. Jos agentin pitää palautua itsestään, käynnistä se oikean valvojan kautta. Älä lisää rinnalle toista prosessia.",
    },
    "health.runtime.title": {
        "en": "Connected agents have no local runtime",
        "fi": "Yhdistetyiltä agenteilta puuttuu paikallinen ajaja",
    },
    "health.runtime.help": {
        "en": "The connection is present, but no local host, spawner or worker was observed. Check run_mode and logs. Start the intended fleet with ./scripts/start_fleet.ps1 (PowerShell) or ./scripts/start_fleet.sh (macOS/Linux).",
        "fi": "Yhteys on olemassa, mutta paikallista hostia, spawneria tai työprosessia ei havaittu. Tarkista run_mode ja lokit. Käynnistys: ./scripts/start_fleet.ps1 (PowerShell) tai ./scripts/start_fleet.sh (macOS/Linux).",
    },
    "health.lock.title": {
        "en": "A lock is held without a visible agent process",
        "fi": "Lukko on varattu ilman näkyvää agenttiprosessia",
    },
    "health.lock.help": {
        "en": "Check the process list, file permissions and the agent log. Identify the lock holder before restarting. Do not delete an actively held lock file.",
        "fi": "Tarkista prosessilista, tiedoston oikeudet ja agentin loki. Selvitä lukon pitäjä ennen uudelleenkäynnistystä. Älä poista käytössä olevaa lukkotiedostoa.",
    },
    "health.down.title": {"en": "Agents are stopped", "fi": "Agentteja on pysähtyneenä"},
    "health.down.help": {
        "en": "This may be intentional. If these agents should run, inspect their logs (l) and run uv run crewaimeat doctor. Check registration and approval before starting the intended fleet runtime.",
        "fi": "Tämä voi olla tarkoituksellista. Jos agenttien pitäisi toimia, tarkista niiden lokit (l) ja aja uv run crewaimeat doctor. Tarkista rekisteröinti ja hyväksyntä ennen oikean ajajan käynnistystä.",
    },
    "health.installed.title": {
        "en": "An installed component could not be verified",
        "fi": "Asennettua osaa ei voitu varmentaa",
    },
    "health.installed.help": {
        "en": "For Python, check uv sync --extra tui. For the CLI, run aimeat --version and check PATH. A timeout also gives this result. The fleet start script handles connector installation; do not replace the global CLI while a fleet is running.",
        "fi": "Python: tarkista uv sync --extra tui. CLI: aja aimeat --version ja tarkista PATH. Myös aikakatkaisu voi aiheuttaa havainnon. Fleetin käynnistysskripti hoitaa liittimen asennuksen; älä korvaa globaalia CLI:tä agenttien ollessa käynnissä.",
    },
    "health.registry.title": {"en": "Latest version could not be checked", "fi": "Uusinta versiota ei voitu tarkistaa"},
    "health.registry.help": {
        "en": "The installed version is known. Check internet access to PyPI/npm and retry. This does not prove the installed component is broken.",
        "fi": "Asennettu versio tunnetaan. Tarkista verkkoyhteys PyPI/npm-palveluihin ja yritä uudelleen. Tämä ei tarkoita, että asennettu osa olisi rikki.",
    },
    "health.update.title": {"en": "An update is available", "fi": "Päivitys on saatavilla"},
    "health.update.help": {
        "en": "Plan the update with a fleet restart. The start_fleet script installs the current connector before launching the service. Review Python dependency updates through uv. An available update alone is not a runtime failure.",
        "fi": "Tee päivitys agenttien uudelleenkäynnistyksen yhteydessä. start_fleet asentaa nykyisen liittimen ennen yhteyspalvelun käynnistystä. Tarkista Python-riippuvuuksien päivitykset uv:n kautta. Pelkkä saatavilla oleva päivitys ei ole ajovika.",
    },
    "intro.hint": {"en": "Enter / Esc: skip    q: quit", "fi": "Enter / Esc: ohita    q: lopeta"},
    "startup.loading": {"en": "Loading fleet...  q: quit", "fi": "Ladataan agentteja...  q: lopeta"},
    "d.loading": {"en": "Loading details...", "fi": "Ladataan lisätietoja..."},
    "d.failed": {"en": "Details unavailable: {error}", "fi": "Lisätietojen lataus epäonnistui: {error}"},
    # status bar + versions
    "sb.watchdogs": {"en": "watchdogs", "fi": "vahdit"},
    "sb.locks": {"en": "locks", "fi": "lukot"},
    "sb.running": {"en": "running", "fi": "ajossa"},
    "sb.stale": {"en": "stale", "fi": "vanhentunut"},
    "sb.threaded": {"en": "threaded", "fi": "säikeinä"},
    "sb.down": {"en": "DOWN", "fi": "ALHAALLA"},
    "ver.loading": {"en": "versions: …", "fi": "versiot: …"},
    # table columns
    "col.agent": {"en": "agent", "fi": "agentti"},
    "col.status": {"en": "status", "fi": "tila"},
    "col.runtime": {"en": "runtime", "fi": "ajaja"},
    "d.runtime": {"en": "runtime", "fi": "ajaja"},
    "col.lock": {"en": "lock", "fi": "lukko"},
    "col.tun": {"en": "tun", "fi": "tun"},
    "col.last_seen": {"en": "last_seen", "fi": "nähty"},
    # detail labels
    "d.agent": {"en": "agent", "fi": "agentti"},
    "d.status": {"en": "status", "fi": "tila"},
    "d.crew_file": {"en": "crew file", "fi": "crew-tiedosto"},
    "d.mode": {"en": "mode", "fi": "moodi"},
    "d.watchdog": {"en": "watchdog", "fi": "vahti"},
    "d.daemon": {"en": "daemon", "fi": "daemon"},
    "d.lock": {"en": "lock", "fi": "lukko"},
    "d.tunnel": {"en": "tunnel", "fi": "tunneli"},
    "d.last_seen": {"en": "last_seen", "fi": "nähty viimeksi"},
    "d.ago": {"en": "ago", "fi": "sitten"},
    "d.none_sel": {"en": "(no agent selected)", "fi": "(ei valittua agenttia)"},
    "d.none": {"en": "(no agents)", "fi": "(ei agentteja)"},
    # sections
    "sec.readme": {"en": "README", "fi": "README"},
    "sec.no_readme": {"en": "(no README)", "fi": "(ei READMEa)"},
    "sec.config": {"en": "config", "fi": "asetukset"},
    "cfg.profile": {"en": "llm profile", "fi": "llm-profiili"},
    "cfg.chain": {"en": "model chain", "fi": "malliketju"},
    "cfg.offers": {"en": "offers", "fi": "tarjoamat"},
    "cfg.wf_compat": {"en": "workflow-compatible", "fi": "workflow-yhteensopiva"},
    "cfg.override": {"en": "model override", "fi": "mallin ohitus"},
    "cfg.override_hint": {"en": "pinned — press m to change", "fi": "kiinnitetty — paina m vaihtaaksesi"},
    "cfg.tags": {"en": "tags", "fi": "tagit"},
    "cfg.cap_technical": {"en": "technical", "fi": "tekninen"},
    "cfg.cap_domain": {"en": "domain", "fi": "toimiala"},
    "cfg.cap_languages": {"en": "languages", "fi": "kielet"},
    "sec.identity": {"en": "identity", "fi": "identiteetti"},
    "sec.contracts": {"en": "contracts", "fi": "sopimukset"},
    "sec.workflows": {"en": "workflows", "fi": "työnkulut"},
    "log.tail": {"en": "log (tail)", "fi": "loki (häntä)"},
    "log.none": {"en": "(no log file)", "fi": "(ei lokitiedostoa)"},
    "log.empty": {"en": "(empty log)", "fi": "(tyhjä loki)"},
    # tabs
    "tab.overview": {"en": "Overview", "fi": "Yleiskuva"},
    "tab.test": {"en": "Test", "fi": "Testi"},
    "tab.config": {"en": "Config", "fi": "Asetukset"},
    "tab.logs": {"en": "Logs", "fi": "Lokit"},
    # test tab
    "test.placeholder": {
        "en": "type a test prompt, press Enter to run against this agent",
        "fi": "kirjoita testikehote, Enter ajaa sen tälle agentille",
    },
    "test.idle": {
        "en": "Select a running agent, type a prompt below and press Enter. A real task is "
        "created and its deliverable is polled — this exercises the live daemon + its model.",
        "fi": "Valitse ajossa oleva agentti, kirjoita kehote ja paina Enter. Luodaan oikea "
        "tehtävä ja sen tulosta pollataan — testaa elävää daemonia + sen mallia.",
    },
    "test.no_agent": {"en": "(no agent selected)", "fi": "(ei valittua agenttia)"},
    "test.not_running": {
        "en": "Agent '{agent}' is not running — start it first (s).",
        "fi": "Agentti '{agent}' ei ole ajossa — käynnistä se ensin (s).",
    },
    "test.busy": {
        "en": "A test is already running — wait for it to finish.",
        "fi": "Testi on jo käynnissä — odota että se valmistuu.",
    },
    "test.running": {"en": "▶ testing {agent}…", "fi": "▶ testataan {agent}…"},
    "test.done": {
        "en": "✓ {agent} responded in {secs}s — task {tid}",
        "fi": "✓ {agent} vastasi {secs}s — tehtävä {tid}",
    },
    "test.failed": {"en": "✗ test failed: {err}", "fi": "✗ testi epäonnistui: {err}"},
    # model picker
    "mp.title": {"en": "Pick a model for '{agent}'", "fi": "Valitse malli agentille '{agent}'"},
    "mp.hint": {
        "en": "[b]enter[/] set + restart   [b]esc[/] cancel   ↑/↓ move",
        "fi": "[b]enter[/] aseta + uudelleen   [b]esc[/] peru   ↑/↓ liiku",
    },
    "mp.clear": {
        "en": "✗ clear override (revert to llm_providers.json routing)",
        "fi": "✗ poista ohitus (palaa llm_providers.json-reititykseen)",
    },
    "mp.none": {"en": "(no models in llm_providers.json)", "fi": "(ei malleja llm_providers.json:ssa)"},
    "mp.set": {"en": "Pinned {agent} → {model}; restarting…", "fi": "Kiinnitetty {agent} → {model}; käynnistetään…"},
    "mp.cleared": {
        "en": "Override cleared for {agent}; restarting…",
        "fi": "Ohitus poistettu agentilta {agent}; käynnistetään…",
    },
    "warn.no_models": {
        "en": "No llm_providers.json models to choose from.",
        "fi": "Ei llm_providers.json-malleja valittavaksi.",
    },
    # confirm modal
    "cf.yes_no": {"en": "[b]y[/] confirm    [b]n[/] / esc cancel", "fi": "[b]y[/] vahvista    [b]n[/] / esc peruuta"},
    "cf.start": {
        "en": "Start crew '{agent}'?  (launch under the watchdog)",
        "fi": "Käynnistä crew '{agent}'?  (watchdogin alle)",
    },
    "cf.stop": {
        "en": "Stop crew '{agent}'?  (kill its watchdog + daemon)",
        "fi": "Pysäytä crew '{agent}'?  (tapa watchdog + daemon)",
    },
    "cf.restart": {
        "en": "Restart crew '{agent}'?  (stop → relaunch)",
        "fi": "Käynnistä crew '{agent}' uudelleen?  (pysäytä → uudelleen)",
    },
    "cf.reauth": {"en": "Re-auth crew '{agent}'?", "fi": "Re-auth crew '{agent}'?"},
    "cf.start_fleet": {
        "en": "Start the WHOLE fleet?  (ensure one serve daemon + launch every approved crew)",
        "fi": "Käynnistä KOKO fleet?  (yksi serve-daemon + kaikki hyväksytyt crewit)",
    },
    "cf.stop_fleet": {
        "en": "STOP the whole fleet?  (kills the serve daemon + every crew)",
        "fi": "PYSÄYTÄ koko fleet?  (tappaa serve-daemonin + kaikki crewit)",
    },
    "cf.restart_fleet": {
        "en": "RESTART the whole fleet?  (stop everything → bring it all back up)",
        "fi": "Käynnistä KOKO fleet uudelleen?  (pysäytä → ylös)",
    },
    "cf.reap": {
        "en": "Reap stray serve daemons (enforce exactly one)?",
        "fi": "Reapaa ylimääräiset serve-daemonit (pakota tasan yksi)?",
    },
    "warn.select": {"en": "Select a crew with a file to {action}.", "fi": "Valitse crew jolla on tiedosto: {action}."},
}


def t(key: str, lang: str = "en") -> str:
    entry = STRINGS.get(key)
    if not entry:
        return key
    return entry.get(lang) or entry.get("en") or key


def default_lang() -> str:
    lang = (os.getenv("AIMEAT_TUI_LANG") or "").strip().lower()
    return lang if lang in LANGS else "en"


def next_lang(cur: str) -> str:
    """Cycle to the next language (en <-> fi)."""
    i = LANGS.index(cur) if cur in LANGS else 0
    return LANGS[(i + 1) % len(LANGS)]
