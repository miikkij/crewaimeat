"""The verify pass checks the deliverable; its verdict is for us, not for the customer who reads it.

Measured 2026-10-02 on a hosted place: deliverables ended with "Verify: faithfulness | score=1 |
unsupported=1 | GDPR claim not found in sources", and carried "[unverified: ...]" marks inside the text.
The check is worth keeping -- it is what removes an invented name, number or citation before anyone
reads it -- but its report is a note to the people who run the agent, and in front of a customer it
reads as the agent doubting itself mid-sentence.

So the reviewer still runs and still ends with its verdict line, and `split_verify` takes the verdict
and the marks OUT of what is published. They are kept where the run is examined: the run log, and the
task's own `verification` event on the node, which is the task's audit trail.

Deterministic on purpose: the reviewer is a model, and a model asked "do not include the verdict" will
include it some of the time. The line it is asked to write has a fixed shape, so code can find it.
"""

from __future__ import annotations

import re

# The verdict lines the scaffold's reviewer is told to end with (aimeat_crew._build): "Verify:
# faithfulness | ...", "Verify: pass", "Verify: fixed - <what>". Only THOSE shapes, so a deliverable's
# own "Verify: the door is locked" in a checklist is left alone. Tolerates the markdown a model wraps
# around a line: a list dash, bold or italics, backticks.
_VERDICT_LINE = re.compile(
    r"^[ \t>*_`-]*Verify:[ \t]*[*_`]*[ \t]*(?:faithfulness\b|pass\b|fixed\b).*$\n?",
    re.IGNORECASE | re.MULTILINE,
)
# "[unverified]" or "[unverified: why]" (any dash or colon after the word).
_MARK = re.compile(r"[ \t]*\[unverified(?:[ \t]*[:\-–—][^\]\n]*)?\]", re.IGNORECASE)
_SCORE = re.compile(r"score\s*=\s*([1-5])", re.IGNORECASE)
_UNSUPPORTED = re.compile(r"unsupported\s*=\s*(\d+)", re.IGNORECASE)


def _clean_line(line: str) -> str:
    return line.strip().strip("*_`").strip().lstrip("->").strip()


def split_verify(text: str) -> tuple[str, dict | None]:
    """(the deliverable without the verify report, the report) -- the report is None when there was none.

    The report: {"verdict": the verdict line(s), "score", "unsupported" (ints or None), "flagged": the
    lines that carried an [unverified] mark, as they read with it}.
    """
    if not text:
        return text, None
    verdicts = [_clean_line(m.group(0)) for m in _VERDICT_LINE.finditer(text)]
    flagged = [_clean_line(line) for line in text.splitlines() if _MARK.search(line)]
    if not verdicts and not flagged:
        return text, None
    body = _VERDICT_LINE.sub("", text)
    body = _MARK.sub("", body)
    # A verdict at the end often comes after a rule or a blank line; take those with it.
    body = re.sub(r"(?:\n[ \t]*(?:-{3,}|\*{3,}|_{3,})?[ \t]*)+\Z", "", body.rstrip()) + (
        "\n" if text.endswith("\n") else ""
    )
    verdict = " / ".join(verdicts)
    score = _SCORE.search(verdict)
    unsupported = _UNSUPPORTED.search(verdict)
    report = {
        "verdict": verdict,
        "score": int(score.group(1)) if score else None,
        "unsupported": int(unsupported.group(1)) if unsupported else None,
        "flagged": flagged,
    }
    return body, report


# A provenance DECLARATION written into the text: "*ai_provenance*: {...}", "AI provenance: ai-generated",
# '"ai_provenance": {...}'. The node's standing directive "Say how content was made ... declare
# `ai_provenance`" is about a WRITE's metadata, and it reaches every task; a domain agent writes nothing
# itself (the runtime publishes its answer), so a model obeying it put the declaration in the answer --
# measured 2026-10-02, the last line a customer read was `*ai_provenance*: {"level":"ai-generated"}`.
_PROVENANCE_LINE = re.compile(r"^[ \t>*_`\"-]*ai[_ -]?provenance[*_`\"]*[ \t]*[:=]", re.IGNORECASE)


def split_provenance(text: str) -> tuple[str, list[str]]:
    """(the text without provenance declarations, the declarations taken out).

    A declaration whose value opens a brace and continues on the next lines is taken out to its close.
    Only whole declaration lines go: a sentence that merely mentions provenance stays.
    """
    if not text or "provenance" not in text.lower():
        return text, []
    kept: list[str] = []
    taken: list[str] = []
    depth = 0
    for line in text.splitlines(keepends=True):
        if depth > 0:
            taken[-1] += line
            depth += line.count("{") - line.count("}")
            continue
        if _PROVENANCE_LINE.match(line):
            taken.append(line)
            depth = max(0, line.count("{") - line.count("}"))
            continue
        kept.append(line)
    if not taken:
        return text, []
    body = "".join(kept)
    # A declaration at the end usually sits under a rule or after a blank line; they go with it.
    body = re.sub(r"(?:\n[ \t]*(?:-{3,}|\*{3,}|_{3,})?[ \t]*)+\Z", "", body.rstrip())
    body += "\n" if text.endswith("\n") else ""
    return body, [t.strip() for t in taken]


# A search tool's "nothing found" wording standing alone as the deliverable's FIRST line, with the real
# answer below it. Measured 2026-10-02 on a hosted place: a customer's reply to "propose an agent" opened
# with "Ei julkista tietoa löytynyt." -- the grounding rule had dictated the phrase, and the model put it
# first before proposing. Only an opener is taken, and only when something follows: a reply that IS
# "nothing was found" is an honest answer and stays.
_STOCK_OPENER = re.compile(
    r"^[ \t>*_`-]*(?:ei (?:julkista )?tietoa löytynyt|ei tuloksia|no (?:public )?information (?:was )?found|"
    r"no results(?: found)?|nothing (?:was )?found|not found)[ \t]*[.!]?[ \t]*[*_`]*[ \t]*\n+",
    re.IGNORECASE,
)


def split_stock_opener(text: str) -> tuple[str, str]:
    """(the text without a tool's no-information phrase on its first line, that phrase or "")."""
    if not text:
        return text, ""
    m = _STOCK_OPENER.match(text)
    if not m or not text[m.end() :].strip():
        return text, ""
    return text[m.end() :], m.group(0).strip().strip("*_`-> ").strip()


def report_message(report: dict) -> str:
    """One line for the task event and the log."""
    msg = report.get("verdict") or "Verify: (no verdict line)"
    if report.get("flagged"):
        msg += f" | {len(report['flagged'])} line(s) carried an [unverified] mark, removed from the deliverable"
    return msg[:4000]
