"""Minimal i18n for the fleet TUI — English + Finnish. `t(key, lang)` returns the string for the
chosen language, falling back to English then the raw key. Only UI CHROME is translated; agent
names, statuses and log text are data and stay as-is. Default language: $AIMEAT_TUI_LANG (en|fi),
else en. Toggle live with the `f` key.
"""

from __future__ import annotations

import os

LANGS = ("en", "fi")

STRINGS: dict[str, dict[str, str]] = {
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
    "col.wd_dae": {"en": "wd/dae", "fi": "vahti/dae"},
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
