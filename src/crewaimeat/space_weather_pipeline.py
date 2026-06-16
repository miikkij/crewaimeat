"""DETERMINISTIC space-weather article — NOAA/spaceweather.com fetch in code, grok writes the prose.

The CrewAI space-weather crew resolved the edition from the dispatched prompt with an LLM and defaulted
to 'am' when it couldn't find one — so on a scheduled 'evening' run it wrote
news.<date>.am.article.avaruussaa and the workflow's evening success signal went output-RED (which
skipped the editorial). Here the edition is a PARAMETER, never guessed: the loop is code, grok writes only
the Finnish article, and it is stored under the exact news.<date>.<edition>.article.avaruussaa key.

`write_space_weather(agent_name, date, edition)` fetches the public sources and writes the article.
"""

from __future__ import annotations

import re

import requests

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.llm import get_llm

_AVARUUSSAA = "avaruussaa"


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _fetch_sources() -> str:
    """Today's PUBLIC space-weather data: NOAA SWPC 3-day forecast (public domain) + spaceweather.com
    narrative (aurora / sunspot / solar-wind context). Facts only — the article reproduces them in Finnish."""
    out = []
    try:
        r = requests.get("https://services.swpc.noaa.gov/text/3-day-forecast.txt", timeout=20)
        r.encoding = "utf-8"
        if r.status_code == 200:
            out.append("=== NOAA SWPC 3-DAY FORECAST (public domain) ===\n" + r.text.strip()[:3500])
    except Exception as e:  # noqa: BLE001
        out.append(f"(NOAA forecast unavailable: {e!r})")
    try:
        r = requests.get("https://spaceweather.com/", timeout=20)
        r.encoding = "utf-8"
        if r.status_code == 200:
            out.append("=== spaceweather.com narrative (facts only) ===\n" + _strip_html(r.text)[:3500])
    except Exception as e:  # noqa: BLE001
        out.append(f"(spaceweather.com unavailable: {e!r})")
    return "\n\n".join(out) if out else "No space-weather sources reachable."


def write_space_weather(agent_name: str, date: str, edition: str) -> str:
    """Fetch NOAA/spaceweather.com and write ONE original Finnish 'Avaruussää tänään' article into
    news.<date>.<edition>.article.avaruussaa (public). The edition is passed in, never guessed."""
    sources = _fetch_sources()
    llm = get_llm(for_tool_use=False, temperature=0.6)
    prompt = (
        "Olet TÄHTI SOINTU, AIMEAT Sanomat'n avaruussää-kirjeenvaihtaja. Kirjoita YKSI alkuperäinen, "
        "selkeä ja hieman ihmettelevä suomenkielinen artikkeli otsikolla 'Avaruussää tänään' näistä "
        "lähteistä. Käsittele: revontulten näkyvyys Suomessa lähiöinä, auringon aktiivisuus, mahdollinen "
        "magneettinen myrsky (Kp-indeksi), aurinkotuuli / koronaaliaukot / purkaukset. Selitä Kp ja "
        "koronaaliaukot kansantajuisesti, älä lietso. Toista FAKTAT omin suomalaisin sanoin (älä kopioi "
        "lähdetekstiä). Aloita otsikolla, lopeta omalle rivilleen '— Tähti Sointu'.\n\nLÄHTEET:\n" + sources)
    art = llm.call([{"role": "user", "content": prompt}])
    art = art if isinstance(art, str) else str(art)
    if len(art.strip()) < 200:  # grok hiccup → one retry
        art = llm.call([{"role": "user", "content": prompt}])
        art = art if isinstance(art, str) else str(art)
    key = f"news.{date}.{edition}.article.{_AVARUUSSAA}"
    _aimeat_call(agent_name, "aimeat_memory_write", {"key": key, "value": art, "visibility": "public"})
    return f"avaruussaa {len(art)} chars -> {key}"


def make_space_weather_tools(agent_name: str) -> list:
    from crewai.tools import tool

    @tool("write_space_weather")
    def write_space_weather_tool(date: str, edition: str) -> str:
        """Deterministically fetch today's NOAA/spaceweather.com data and write the Finnish 'Avaruussää
        tänään' article into news.<date>.<edition>.article.avaruussaa (public). Call ONCE with the resolved
        date+edition — the edition is used verbatim, never guessed. Returns a short report."""
        return write_space_weather(agent_name, (date or "").strip(), (edition or "").strip())

    write_space_weather_tool.cache_function = lambda *_a, **_k: False
    return [write_space_weather_tool]
