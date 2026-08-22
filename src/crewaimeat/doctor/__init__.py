"""`crewaimeat doctor` — continuous reconciliation of what this repo DECLARES against what it DOES.

Three lenses, deliberately different in kind, because the failures they catch are different in kind:

  1. registries   set reconciliation — six hand-kept lists must agree about which agents exist
  2. conformance  a call-graph route check — does code reach the node / a model through the
                  sanctioned path, and is any failure on the deliverable path visible
  3. liveness     what the NODE believes, versus what this repo declares (opt-in, needs the fleet)

A linter cannot do 1 or 3 at all: it reads one file and knows nothing about serve.json, the routing
map, or the node. It could in principle do 2, but a lint rule is written against a syntax pattern
while these are written against an EDGE — "this call reaches the node without passing through the
dispatcher" is a statement about the graph, not about the line.

Run it from the CLI (`crewaimeat doctor`), from CI (`--strict`), from a pre-commit hook, or from the
fleet's start-up, which prints the report and keeps going.
"""

from __future__ import annotations

from .cli import main, run
from .model import ERROR, WARN, Finding, Report

__all__ = ["ERROR", "WARN", "Finding", "Report", "main", "run"]
