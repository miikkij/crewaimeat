"""Jaetut AIMEAT-task-runner -apurit.

crewfive-kruut voidaan käynnistää aliprosessina `aimeat connect serve`:n toimesta.
Tämä moduuli hoitaa sopimuksen AIMEATin kanssa:

- lukee tehtävän env-muuttujista (AIMEAT_TASK_PROMPT jne.)
- muotoilee lopputuloksen yhtenäiseksi Deliverable-JSON:ksi
- tulostaa sen stdoutiin ja/tai tiedostoon (serve nappaa tämän)
- voi kesken ajon kirjoittaa muistiinpanon takaisin AIMEATiin CLI:n kautta
  (`aimeat connect call aimeat_memory_write ...`), best-effort.

Mitään AIMEAT-Python-pakettia ei tarvita – pelkkä subprocess + CLI riittää.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from pydantic import BaseModel, Field

# Käytetään samaa oletustehtävää kuin paikallisessa CLI:ssä, jotta skriptit
# ovat ajettavissa myös ilman AIMEATia (standalone-testaus).
DEFAULT_REQUEST = (
    "Laadi liiketoimintasuunnitelma uudelle B2B SaaS -tuotteelle, joka auttaa "
    "pk-yrityksiä automatisoimaan laskutuksensa tekoälyn avulla. Kata teknologia, "
    "markkinointi, talous ja operaatiot."
)


# --------------------------------------------------------------------------- #
# Deliverable-malli (doc Task 4: { title, summary, sections, recommendations })
# --------------------------------------------------------------------------- #
class Section(BaseModel):
    heading: str = Field(description="Osion otsikko")
    content: str = Field(description="Osion sisältö")


class Deliverable(BaseModel):
    title: str = Field(description="Lyhyt otsikko lopputulokselle")
    summary: str = Field(description="Tiivistelmä lopputuloksesta")
    sections: list[Section] = Field(
        default_factory=list, description="Raportin osiot"
    )
    recommendations: list[str] = Field(
        default_factory=list, description="Konkreettiset suositukset"
    )


# --------------------------------------------------------------------------- #
# Env-sopimus
# --------------------------------------------------------------------------- #
@dataclass
class RunnerEnv:
    prompt: str
    task_id: str | None
    agent_name: str | None
    token: str | None


def read_runner_env() -> RunnerEnv:
    """Lukee AIMEAT-task-runnerin env-muuttujat.

    Jos AIMEAT_TASK_PROMPT puuttuu, käytetään CLI-argumentteja tai oletustehtävää,
    jotta skripti on ajettavissa myös standalone-testinä.
    """
    prompt = os.getenv("AIMEAT_TASK_PROMPT", "").strip()
    if not prompt:
        prompt = " ".join(sys.argv[1:]).strip() or DEFAULT_REQUEST
    return RunnerEnv(
        prompt=prompt,
        task_id=os.getenv("AIMEAT_TASK_ID") or None,
        agent_name=os.getenv("AIMEAT_AGENT_NAME") or None,
        token=os.getenv("AIMEAT_TOKEN") or None,
    )


# --------------------------------------------------------------------------- #
# Lopputuloksen muotoilu ja tulostus
# --------------------------------------------------------------------------- #
def emit_deliverable(deliverable: Deliverable) -> None:
    """Tulostaa Deliverablen JSON:na.

    - Jos CREW_OUTPUT_FILE on asetettu, kirjoittaa JSON:n sinne (output_capture: file:<path>).
    - Tulostaa JSON:n aina myös stdoutin viimeiseksi (output_capture: stdout).
    """
    payload = deliverable.model_dump()
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    out_file = os.getenv("CREW_OUTPUT_FILE", "").strip()
    if out_file:
        from pathlib import Path

        path = Path(out_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"[crewfive] Deliverable kirjoitettu: {path}", file=sys.stderr)

    # Lopullinen JSON stdoutiin (serve nappaa tämän stdout-capture-moodissa).
    print(text)


def _parse_markdown_sections(raw: str) -> list[Section]:
    """Pilkkoo markdown-tekstin osioihin '##'/'###'-otsikoiden perusteella.

    Robusti: jos otsikoita ei löydy, palauttaa yhden osion koko tekstillä.
    """
    if not raw:
        return []
    lines = raw.splitlines()
    sections: list[Section] = []
    heading = None
    buf: list[str] = []

    def flush() -> None:
        if heading is not None or buf:
            content = "\n".join(buf).strip()
            sections.append(Section(heading=heading or "Yhteenveto", content=content))

    for line in lines:
        m = re.match(r"^#{2,4}\s+(.*)$", line.strip())
        if m:
            flush()
            heading = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    flush()

    # Jos koko teksti oli otsikoton, palautetaan yksi osio.
    if not sections:
        return [Section(heading="Yhteenveto", content=raw.strip())]
    return sections


def coerce_deliverable(result, prompt: str) -> Deliverable:
    """Muuntaa CrewAI:n kickoff-tuloksen Deliverableksi.

    Yritysjärjestys: valmis pydantic-malli -> JSON-dict -> raaka markdown käärittynä.
    """
    # 1) Task määritteli output_pydantic=Deliverable.
    pyd = getattr(result, "pydantic", None)
    if isinstance(pyd, Deliverable):
        return pyd

    # 2) Strukturoitu JSON-dict tuloksessa.
    data = getattr(result, "json_dict", None)
    if isinstance(data, dict) and data.get("title"):
        try:
            return Deliverable.model_validate(data)
        except Exception:
            pass

    # 3) Fallback: käärit raaka teksti.
    raw = getattr(result, "raw", None) or str(result)
    title = prompt.strip().splitlines()[0][:80] if prompt.strip() else "Lopputulos"
    sections = _parse_markdown_sections(raw)

    # Poimi suositukset osiosta, jonka otsikko viittaa suosituksiin.
    recommendations: list[str] = []
    for sec in sections:
        if re.search(r"suosit|recommend|toimenpit", sec.heading, re.IGNORECASE):
            for line in sec.content.splitlines():
                item = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()
                if item:
                    recommendations.append(item)

    return Deliverable(
        title=title,
        summary=(raw[:600] + "…") if len(raw) > 600 else raw,
        sections=sections,
        recommendations=recommendations,
    )


# --------------------------------------------------------------------------- #
# Takaisinkutsu AIMEATiin (best-effort, CLI-fallback)
# --------------------------------------------------------------------------- #
def write_memory_note(
    key: str, value, tags: list[str] | None = None, visibility: str = "private"
) -> bool:
    """Kirjoittaa muistiinpanon AIMEATiin: `aimeat connect call aimeat_memory_write`.

    Best-effort: jos `aimeat`-CLI:tä ei löydy tai kutsu epäonnistuu, palautetaan
    False eikä kruun ajoa kaadeta. Autentikointi tapahtuu AIMEAT-CLI:n omasta
    konfiguraatiosta (~/.aimeat/), ei tästä prosessista.
    """
    exe = shutil.which("aimeat")
    if exe is None:
        print(
            "[crewfive] 'aimeat'-CLI ei löydy PATHista – ohitetaan muistiinpano.",
            file=sys.stderr,
        )
        return False

    payload: dict = {"key": key, "value": value, "visibility": visibility}
    if tags:
        payload["tags"] = tags

    args = [exe, "connect", "call", "aimeat_memory_write", "--json", json.dumps(payload)]
    # Windowsissa npm-asennettu `aimeat` on .cmd/.bat – se vaatii shellin.
    use_shell = os.name == "nt" and exe.lower().endswith((".cmd", ".bat"))
    cmd = subprocess.list2cmdline(args) if use_shell else args

    try:
        proc = subprocess.run(
            cmd,
            shell=use_shell,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 – best-effort, ei saa kaataa kruuta
        print(f"[crewfive] aimeat_memory_write epäonnistui: {exc}", file=sys.stderr)
        return False

    if proc.returncode != 0:
        print(
            f"[crewfive] aimeat_memory_write palautti {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}",
            file=sys.stderr,
        )
        return False

    print(f"[crewfive] Muistiinpano kirjoitettu AIMEATiin: {key}", file=sys.stderr)
    return True


def force_utf8_stdout() -> None:
    """Pakottaa stdout/stderr UTF-8:ksi (Windows-konsolin cp1252-ongelma)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
