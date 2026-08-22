"""Lens 2 — ROUTE CONFORMANCE. Does the code reach the outside world through the sanctioned routes?

This is a call-graph conformance check, not a linter. A linter reads one file and asks "is this
statement well formed"; this reads the whole package and asks "does this EDGE exist, and is it allowed
to". Those are different questions, and only the second one catches the failures that actually hurt
here — a node write that skips the shared dispatcher (and so its auth, its retry policy and its loud
failure), a crew that builds its own LLM and escapes routing, a version literal that makes a second
source of truth, an exception handler that turns a failed publish into a green run.

Every rule states the ROUTE and the BYPASS, because a rule whose violation you cannot picture is a rule
people disable. Existing bypasses are recorded in doctor-baseline.json: they stop failing the build,
and no NEW one may be added. That is the ratchet — the point is not to be clean today, it is to be
strictly cleaner tomorrow than today.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .model import ERROR, WARN, Finding, Report

# ── Route 1: everything that touches the NODE goes through the shared dispatcher ────────────────────
# `_aimeat_call` (MCP tools) and `_aimeat_rest` (/v1 routes) own the transport: the loopback serve
# daemon's tunnel, the agent header, the retry/backoff policy, and a LOUD failure. A hand-rolled
# requests call to the node re-implements all four, and historically got them wrong in the same way
# every time — a swallowed 401 that looks like "no tasks" (see run-crew-daemon-real-behavior).
DISPATCHER_MODULES = {
    "src/crewaimeat/aimeat_crew.py",  # defines _aimeat_call / _aimeat_rest
    "src/crewaimeat/serve_guard.py",  # owns the daemon lifecycle, must probe it directly
    "src/crewaimeat/serve_watchdog.py",
    "src/crewaimeat/wake_spin.py",  # reads the daemon's wake queue by design
    "src/crewaimeat/node_engine.py",
    "src/crewaimeat/tui/versions.py",  # asks npm/PyPI, not the node
}
HTTP_VERBS = {"get", "post", "put", "patch", "delete", "request", "head", "stream"}
HTTP_LIBS = {"requests", "httpx", "urllib"}
# A URL argument mentioning any of these is aimed at the AIMEAT node rather than a third-party API.
NODE_URL_MARKERS = ("/v1/", "node_url", "NODE_URL", "aimeat.io", "AIMEAT_URL")

# ── Route 2: a crew's model comes from routing, never from a constructor ────────────────────────────
LLM_CONSTRUCTORS = {"LLM", "ChatOpenAI", "ChatAnthropic", "AzureChatOpenAI", "MultiProviderLLM"}

# ── Route 3: a failure on the deliverable/node path must be seen ────────────────────────────────────
# Names that WRITE something the owner is expecting. A swallowed failure here is a green run with no
# result — the single worst failure mode this repo has, and the one CLAUDE.md's "fail loud" exists for.
SINK_RE = re.compile(
    r"^_?(pub|publish|prev|write|report|complete|deliver|contribute|_aimeat_call|_aimeat_rest)",
    re.IGNORECASE,
)

CONNECTOR_LITERAL = re.compile(r"aimeat@\d+\.\d+\.\d+")


def _scope_map(tree: ast.Module) -> dict[int, str]:
    """line -> the dotted name of the function/class that owns it.

    Findings are keyed by their SUBJECT, and the baseline matches on that key. `file.py:412` breaks the
    moment anyone inserts a line above it: the same untouched violation reads as a brand-new finding AND
    leaves a stale baseline entry, so every refactor produces churn and the ratchet gets switched off
    within a month. `file.py::outer.inner` survives ordinary editing and still points a human at the
    right place — the exact line goes in the message, where being approximate costs nothing.
    """
    scopes: dict[int, str] = {}

    def walk(node, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                for line in range(child.lineno, (getattr(child, "end_lineno", child.lineno) or child.lineno) + 1):
                    scopes[line] = name
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, "")
    return scopes


def _where(rel: str, scopes: dict[int, str], lineno: int) -> str:
    scope = scopes.get(lineno)
    return f"{rel}::{scope}" if scope else f"{rel}::<module>"


def _rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def _http_aliases(tree: ast.Module) -> set[str]:
    """Names in this module that are HTTP clients — resolved from the imports, never guessed.

    Guessing is what makes this kind of check useless: matching any `.get(` call flags every dict
    lookup in the file and the whole rule gets ignored as noise.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in HTTP_LIBS:
                    aliases.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in HTTP_LIBS:
            for a in node.names:
                aliases.add(a.asname or a.name)
        # `s = requests.Session()` / `session = httpx.Client()` — the object is an HTTP client too.
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr in {"Session", "Client"}
                and isinstance(fn.value, ast.Name)
                and fn.value.id in HTTP_LIBS
            ):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        aliases.add(t.id)
    return aliases


def _is_http_call(node: ast.Call, aliases: set[str]) -> bool:
    fn = node.func
    if isinstance(fn, ast.Attribute) and fn.attr in HTTP_VERBS:
        base = fn.value
        return isinstance(base, ast.Name) and base.id in aliases
    return isinstance(fn, ast.Name) and fn.id in aliases and fn.id.startswith("urlopen")


def _targets_node(node: ast.Call, src_lines: list[str]) -> bool:
    """Is the URL argument aimed at the AIMEAT node? Read the call's own source span, so a marker
    three lines away in an unrelated statement cannot produce a false positive."""
    start = node.lineno - 1
    end = getattr(node, "end_lineno", node.lineno)
    span = "\n".join(src_lines[start:end])
    return any(m in span for m in NODE_URL_MARKERS)


def _handler_is_silent(handler: ast.ExceptHandler) -> bool:
    """A handler whose entire body discards the error: `pass` / `continue` / a bare `return`."""
    body = [n for n in handler.body if not isinstance(n, ast.Expr) or not isinstance(n.value, ast.Constant)]
    if len(body) != 1:
        return False
    only = body[0]
    if isinstance(only, (ast.Pass, ast.Continue, ast.Break)):
        return True
    return isinstance(only, ast.Return) and (only.value is None or isinstance(only.value, ast.Constant))


def _sink_calls(try_body: list[ast.stmt]) -> list[str]:
    """Names called in the guarded body that WRITE something someone is waiting for."""
    found = []
    for stmt in try_body:
        for n in ast.walk(stmt):
            if not isinstance(n, ast.Call):
                continue
            fn = n.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if name and SINK_RE.match(name):
                found.append(name)
    return found


def check(root: Path, report: Report) -> None:
    files = sorted(list((root / "src" / "crewaimeat").rglob("*.py")) + list((root / "crews").glob("*.py")))
    scanned = 0
    for path in files:
        rel = _rel(path, root)
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, SyntaxError) as exc:
            report.add(
                Finding(
                    "conformance.unparsable",
                    ERROR,
                    rel,
                    f"cannot be parsed, so no route rule can be checked against it: {exc}",
                    "fix the syntax error",
                )
            )
            continue
        scanned += 1
        lines = text.splitlines()
        in_crew = rel.startswith("crews/")
        aliases = _http_aliases(tree)
        scopes = _scope_map(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                _check_node_route(node, aliases, lines, rel, scopes, in_crew, report)
                _check_llm_route(node, rel, scopes, in_crew, report)
            elif isinstance(node, ast.Try):
                _check_silent_guard(node, rel, scopes, report)
        _check_version_literal(text, lines, rel, scopes, report)
    report.note(f"route conformance: {scanned} files scanned, {len(files) - scanned} unparsable")


def _check_node_route(
    node: ast.Call,
    aliases: set[str],
    lines: list[str],
    rel: str,
    scopes: dict[int, str],
    in_crew: bool,
    report: Report,
) -> None:
    if rel in DISPATCHER_MODULES or not aliases:
        return
    if not _is_http_call(node, aliases) or not _targets_node(node, lines):
        return
    report.add(
        Finding(
            "route.node.direct_http",
            ERROR if in_crew else WARN,
            _where(rel, scopes, node.lineno),
            f"line {node.lineno}: talks to the AIMEAT node over raw HTTP instead of the shared dispatcher, so it re-implements "
            "the agent auth, the tunnel and the retry/backoff — and a 401 here looks like an empty result"
            + (". A crew must never reach the node directly; the scaffold owns that." if in_crew else ""),
            "call _aimeat_call (MCP tools) or _aimeat_rest (/v1 routes) from crewaimeat.aimeat_crew",
        )
    )


def _check_llm_route(node: ast.Call, rel: str, scopes: dict[int, str], in_crew: bool, report: Report) -> None:
    if not in_crew:
        return
    fn = node.func
    name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
    if name not in LLM_CONSTRUCTORS:
        return
    report.add(
        Finding(
            "route.llm.direct",
            ERROR,
            _where(rel, scopes, node.lineno),
            f"line {node.lineno}: constructs {name}(...) directly, which escapes llm_providers.json routing entirely — the "
            "crew's model then ignores its profile, its per-agent override and the fallback chain",
            "use ctx.llm, or get_llm(agent_name=AGENT_NAME) when a second model is genuinely needed",
        )
    )


def _check_silent_guard(node: ast.Try, rel: str, scopes: dict[int, str], report: Report) -> None:
    sinks = _sink_calls(node.body)
    if not sinks:
        return
    for handler in node.handlers:
        if not _handler_is_silent(handler):
            continue
        report.add(
            Finding(
                "guard.silent_sink",
                WARN,
                _where(rel, scopes, handler.lineno),
                f"line {handler.lineno}: discards any failure of {', '.join(sorted(set(sinks))[:3])}() without a word — a write "
                "someone is waiting for can fail here and the run still reports success",
                "log the exception (loud) and, on the deliverable path, let it propagate",
            )
        )


def _check_version_literal(text: str, lines: list[str], rel: str, scopes: dict[int, str], report: Report) -> None:
    """The connector version may exist in exactly ONE place. Four places is how 2.0.0, 2.6.1, 3.3.2 and
    1.34.0 all became "the version" at the same time."""
    for i, line in enumerate(lines, start=1):
        m = CONNECTOR_LITERAL.search(line)
        if not m:
            continue
        if rel == "src/crewaimeat/forge.py" and line.strip().startswith("AIMEAT_CONNECTOR"):
            continue  # the one source of truth
        hash_at = line.find("#")
        if hash_at != -1 and hash_at < m.start():
            continue  # inside a comment — history or an example, not a second source of truth
        report.add(
            Finding(
                "guard.version_literal",
                ERROR,
                _where(rel, scopes, i),
                f"line {i}: hardcodes a connector version, creating a second source of truth that drifts silently",
                "read forge.AIMEAT_CONNECTOR instead",
            )
        )
