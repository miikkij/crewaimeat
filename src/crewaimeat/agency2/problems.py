"""Something failed: keep the whole cause, and show the person words plus a way to it.

A failure's full text (exception type and message) is written to `.agency2/logs/problems.log` under a
short reference. What a response carries is a sentence chosen from this module's own table — decided
by WHAT failed (a refused key, a daemon that does not answer, a model call), never copied out of the
exception — and the reference. The UI turns `[ref:…]` into "Show details", which reads the log line
back through `/api/problems/<ref>`.

Why not just return `str(exc)`: CodeQL (py/stack-trace-exposure, alerts #43-#49) flags every response
built from an exception. The cause is still one click away — fail loud stays — but an exception's text
no longer flows straight into an HTTP answer.
"""

from __future__ import annotations

import json
import time
import uuid

from crewaimeat.agency2 import paths

_LOG_NAME = "problems.log"
_REF_CHARS = frozenset("0123456789abcdef")

_WORDS = {
    "fi": {
        "auth": "palvelin hylkäsi agentin tunnuksen",
        "scope": "agentilta puuttuu tähän tarvittava oikeus",
        "not_found": "palvelimelta ei löytynyt haettua tietoa",
        "refused": "palvelin kieltäytyi",
        "no_daemon": "yhteyspalvelu ei vastaa",
        "unreachable": "palvelin ei vastaa",
        "model": "mallin kutsu epäonnistui",
        "read": "tietoa ei voitu lukea",
        "invalid": "määritelmä ei läpäissyt tarkistusta — kokeile kuvata tehtävä toisin",
        "other": "jokin meni vikaan",
    },
    "en": {
        "auth": "the instance refused the agent's key",
        "scope": "the agent lacks a permission this needs",
        "not_found": "the instance did not have what was asked for",
        "refused": "the instance refused",
        "no_daemon": "the connection service does not answer",
        "unreachable": "the instance does not answer",
        "model": "the model call failed",
        "read": "it could not be read",
        "invalid": "the definition did not pass the check — try describing the job differently",
        "other": "something went wrong",
    },
}


def _log():
    return paths.contained(paths.logs_dir() / _LOG_NAME)


def _write(where: str, type_: str, text: str) -> str:
    ref = uuid.uuid4().hex[:8]
    row = {"ref": ref, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "where": where, "type": type_, "text": text}
    with open(_log(), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return ref


def note(exc: BaseException, where: str) -> str:
    """Write the whole cause down; return its reference (random — nothing of the exception in it)."""
    return _write(where, type(exc).__name__, str(exc))


def say_invalid(errors: list[str], where: str, lang: str = "fi") -> str:
    """A definition the validator refused: a sentence + a reference to the validator's own lines.

    The lines are written for the MODEL (fed back to it as correction) and some quote a validator
    exception (crew_def._validate_signal_tree); the person gets a sentence and opens the lines behind
    the reference if they want them."""
    ref = _write(where, "ValidationError", "\n".join(errors))
    return f"{_WORDS.get(lang, _WORDS['en'])['invalid']} [ref:{ref}]"


def kind_of(exc: BaseException) -> str:
    """Which sentence fits — decided by the failure's type and status, not by its text."""
    from crewaimeat.agency2 import node

    if isinstance(exc, node.NoDaemon):
        return "no_daemon"
    if isinstance(exc, node.Refused):
        if exc.status in (401,):
            return "auth"
        if exc.status == 403 or exc.code in ("SCOPE_DENIED", "INSUFFICIENT_SCOPE", "ACCESS_DENIED"):
            return "scope"
        if exc.status == 404:
            return "not_found"
        return "refused"
    return "other"


def say(exc: BaseException, where: str, lang: str = "fi", kind: str | None = None) -> str:
    """The person's sentence + the reference to the whole cause."""
    ref = note(exc, where)
    words = _WORDS.get(lang, _WORDS["en"])
    return f"{words[kind or kind_of(exc)]} [ref:{ref}]"


def detail(ref: str) -> dict | None:
    """The logged row for `ref`, read back from the log (newest first), or None."""
    if len(ref) != 8 or not set(ref) <= _REF_CHARS:
        return None
    try:
        lines = _log().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines[-2000:]):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("ref") == ref:
            return row
    return None
