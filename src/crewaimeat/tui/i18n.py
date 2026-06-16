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
    "log.tail": {"en": "log (tail)", "fi": "loki (häntä)"},
    "log.none": {"en": "(no log file)", "fi": "(ei lokitiedostoa)"},
    "log.empty": {"en": "(empty log)", "fi": "(tyhjä loki)"},
    # tabs
    "tab.overview": {"en": "Overview", "fi": "Yleiskuva"},
    "tab.config": {"en": "Config", "fi": "Asetukset"},
    "tab.logs": {"en": "Logs", "fi": "Lokit"},
    # confirm modal
    "cf.yes_no": {"en": "[b]y[/] confirm    [b]n[/] / esc cancel",
                  "fi": "[b]y[/] vahvista    [b]n[/] / esc peruuta"},
    "cf.start": {"en": "Start crew '{agent}'?  (launch under the watchdog)",
                 "fi": "Käynnistä crew '{agent}'?  (watchdogin alle)"},
    "cf.stop": {"en": "Stop crew '{agent}'?  (kill its watchdog + daemon)",
                "fi": "Pysäytä crew '{agent}'?  (tapa watchdog + daemon)"},
    "cf.restart": {"en": "Restart crew '{agent}'?  (stop → relaunch)",
                   "fi": "Käynnistä crew '{agent}' uudelleen?  (pysäytä → uudelleen)"},
    "cf.reauth": {"en": "Re-auth crew '{agent}'?", "fi": "Re-auth crew '{agent}'?"},
    "cf.start_fleet": {"en": "Start the WHOLE fleet?  (ensure one serve daemon + launch every approved crew)",
                       "fi": "Käynnistä KOKO fleet?  (yksi serve-daemon + kaikki hyväksytyt crewit)"},
    "cf.stop_fleet": {"en": "STOP the whole fleet?  (kills the serve daemon + every crew)",
                      "fi": "PYSÄYTÄ koko fleet?  (tappaa serve-daemonin + kaikki crewit)"},
    "cf.restart_fleet": {"en": "RESTART the whole fleet?  (stop everything → bring it all back up)",
                         "fi": "Käynnistä KOKO fleet uudelleen?  (pysäytä → ylös)"},
    "cf.reap": {"en": "Reap stray serve daemons (enforce exactly one)?",
                "fi": "Reapaa ylimääräiset serve-daemonit (pakota tasan yksi)?"},
    "warn.select": {"en": "Select a crew with a file to {action}.",
                    "fi": "Valitse crew jolla on tiedosto: {action}."},
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
