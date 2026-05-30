"""Shared AIMEAT task-runner helpers.

crewfive crews can be launched as a subprocess by `aimeat connect serve`. This
module handles the contract with AIMEAT:

- reads the task from env vars (AIMEAT_TASK_PROMPT etc.)
- formats the result into a uniform Deliverable JSON
- prints it to stdout and/or a file (serve captures this)
- can write a note back to AIMEAT mid-run via the CLI
  (`aimeat connect call aimeat_memory_write ...`), best-effort.

No AIMEAT Python package is needed – a plain subprocess + CLI is enough.
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

# Use the same default task as the local CLI so the scripts can be run without
# AIMEAT too (standalone testing).
DEFAULT_REQUEST = (
    "Draft a business plan for a new B2B SaaS product that helps SMEs automate "
    "their invoicing with AI. Cover technology, marketing, finance and operations."
)


# --------------------------------------------------------------------------- #
# Deliverable model (doc Task 4: { title, summary, sections, recommendations })
# --------------------------------------------------------------------------- #
class Section(BaseModel):
    heading: str = Field(description="Section heading")
    content: str = Field(description="Section content")


class Deliverable(BaseModel):
    title: str = Field(description="Short title for the result")
    summary: str = Field(description="Summary of the result")
    sections: list[Section] = Field(
        default_factory=list, description="Report sections"
    )
    recommendations: list[str] = Field(
        default_factory=list, description="Concrete recommendations"
    )


# --------------------------------------------------------------------------- #
# Env contract
# --------------------------------------------------------------------------- #
@dataclass
class RunnerEnv:
    prompt: str
    task_id: str | None
    agent_name: str | None
    token: str | None


def read_runner_env() -> RunnerEnv:
    """Read the AIMEAT task-runner env vars.

    If AIMEAT_TASK_PROMPT is missing, fall back to CLI args or the default task
    so the script can also be run as a standalone test.
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
# Result formatting and output
# --------------------------------------------------------------------------- #
def emit_deliverable(deliverable: Deliverable) -> None:
    """Print the Deliverable as JSON.

    - If CREW_OUTPUT_FILE is set, write the JSON there (output_capture: file:<path>).
    - Always print the JSON last to stdout too (output_capture: stdout).
    """
    payload = deliverable.model_dump()
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    out_file = os.getenv("CREW_OUTPUT_FILE", "").strip()
    if out_file:
        from pathlib import Path

        path = Path(out_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"[crewfive] Deliverable written: {path}", file=sys.stderr)

    # Final JSON to stdout (serve captures this in stdout-capture mode).
    print(text)


def _parse_markdown_sections(raw: str) -> list[Section]:
    """Split markdown text into sections based on '##'/'###' headings.

    Robust: if no headings are found, return a single section with the whole text.
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
            sections.append(Section(heading=heading or "Summary", content=content))

    for line in lines:
        m = re.match(r"^#{2,4}\s+(.*)$", line.strip())
        if m:
            flush()
            heading = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    flush()

    # If the whole text was heading-less, return a single section.
    if not sections:
        return [Section(heading="Summary", content=raw.strip())]
    return sections


def coerce_deliverable(result, prompt: str) -> Deliverable:
    """Convert CrewAI's kickoff result into a Deliverable.

    Try order: ready pydantic model -> JSON dict -> raw markdown wrapped.
    """
    # 1) The task declared output_pydantic=Deliverable.
    pyd = getattr(result, "pydantic", None)
    if isinstance(pyd, Deliverable):
        return pyd

    # 2) Structured JSON dict in the result.
    data = getattr(result, "json_dict", None)
    if isinstance(data, dict) and data.get("title"):
        try:
            return Deliverable.model_validate(data)
        except Exception:
            pass

    # 3) Fallback: wrap raw text.
    raw = getattr(result, "raw", None) or str(result)
    title = prompt.strip().splitlines()[0][:80] if prompt.strip() else "Result"
    sections = _parse_markdown_sections(raw)

    # Pick recommendations from a section whose heading hints at recommendations.
    # Keep both English and Finnish keywords so either output language is handled.
    recommendations: list[str] = []
    for sec in sections:
        if re.search(r"recommend|suosit|toimenpit|action", sec.heading, re.IGNORECASE):
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
# Callback to AIMEAT (best-effort, CLI fallback)
# --------------------------------------------------------------------------- #
def write_memory_note(
    key: str, value, tags: list[str] | None = None, visibility: str = "private"
) -> bool:
    """Write a note to AIMEAT: `aimeat connect call aimeat_memory_write`.

    Best-effort: if the `aimeat` CLI is not found or the call fails, return
    False and do not crash the crew. Authentication comes from the AIMEAT CLI's
    own configuration (~/.aimeat/), not from this process.
    """
    exe = shutil.which("aimeat")
    if exe is None:
        print(
            "[crewfive] 'aimeat' CLI not found in PATH – skipping memory note.",
            file=sys.stderr,
        )
        return False

    payload: dict = {"key": key, "value": value, "visibility": visibility}
    if tags:
        payload["tags"] = tags

    args = [exe, "connect", "call", "aimeat_memory_write", "--json", json.dumps(payload)]
    # On Windows the npm-installed `aimeat` is a .cmd/.bat – it needs a shell.
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
    except Exception as exc:  # noqa: BLE001 – best-effort, must not crash the crew
        print(f"[crewfive] aimeat_memory_write failed: {exc}", file=sys.stderr)
        return False

    if proc.returncode != 0:
        print(
            f"[crewfive] aimeat_memory_write returned {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}",
            file=sys.stderr,
        )
        return False

    print(f"[crewfive] Memory note written to AIMEAT: {key}", file=sys.stderr)
    return True


def force_utf8_stdout() -> None:
    """Force stdout/stderr to UTF-8 (Windows console cp1252 issue)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
