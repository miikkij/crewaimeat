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


def report_message(report: dict) -> str:
    """One line for the task event and the log."""
    msg = report.get("verdict") or "Verify: (no verdict line)"
    if report.get("flagged"):
        msg += f" | {len(report['flagged'])} line(s) carried an [unverified] mark, removed from the deliverable"
    return msg[:4000]
