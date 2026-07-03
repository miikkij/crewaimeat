"""research-contract: a DETERMINISTIC workspace-contract processor for the `research` capability.

The contract (the convention any organism workspace can adopt):
  inputs : `research-request` (records)  — trigger: status == 'requested'
             { id, brief(required), depth?, focus?, status, requested_by?, result_ref?, error? }
  outputs: `research-result`  (DOCUMENT) — a markdown note { title, markdown } (id = res-<request id>),
             so the distilled research renders as a proper page, not a record field.
  lifecycle: requested -> in-progress (claim) -> done (+result_ref) | failed (+error)

The loop is plain code (discover -> claim -> work -> write -> advance); only the distillation of the
fetched pages into a useful note is the LLM's job. The agent (web-researcher) processes EVERY workspace
it is a member of that declares a `research-request` space — so the same agent serves many organisms.
It never posts anywhere external; it only reads + writes the workspace.
"""

from __future__ import annotations

import datetime
import sys

from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call, member_workspaces
from crewaimeat.article_extract import _MIN_CHARS, _trafilatura_text
from crewaimeat.fetch_pipeline import _searxng_urls
from crewaimeat.llm import get_llm

AGENT = "web-researcher"
IN_SPACE, IN_NS = "research-request", "shared.research_requests"
OUT_SPACE, OUT_NS = "research-result", "shared.research_docs"  # a DOCUMENT space (fresh namespace)

# Machine-readable contract declaration (§2) — what adopt-contract provisions into a workspace.
CONTRACT = {
    "id": "research",
    "spaces": [
        {
            "space": IN_SPACE,
            "namespace": IN_NS,
            "mode": "records",
            "schema": {
                "type": "object",
                "required": ["id", "brief", "status"],
                "properties": {
                    "id": {"type": "string"},
                    "brief": {"type": "string"},
                    "depth": {"type": "integer"},
                    "focus": {"type": "string"},
                    "requested_by": {"type": "string"},
                    "result_ref": {"type": "string"},
                    "error": {"type": "string"},
                    "status": {"type": "string", "enum": ["requested", "in-progress", "done", "failed"]},
                },
            },
        },
        {"space": OUT_SPACE, "namespace": OUT_NS, "mode": "document"},
    ],
}

# Runaway guard: request ids already handled in THIS daemon run. Prevents re-processing the same request
# if a stale read keeps showing status=='requested' after we advanced it (read-after-write lag). Resets on
# daemon restart, by when the read is consistent (status=='done' -> skipped normally).
_PROCESSED: set[str] = set()


def _call(tool_name: str, payload: dict):
    return _aimeat_call(AGENT, tool_name, payload)


def _member_workspaces() -> list[tuple[str, str]]:
    """Workspaces this agent serves (organism_list + AIMEAT_CONTRACT_ORGS home organisms)."""
    return member_workspaces(AGENT)


def do_research(brief: str, depth: int = 5, focus: str = "") -> tuple[str | None, list[str]]:
    """Search the web, fetch the top pages, and distill a factual research note. Returns (summary_md, sources)."""
    query = f"{brief} {focus}".strip()
    urls = _searxng_urls(query, "en", "month", n=max(depth * 2, 8)) or []
    docs: list[dict] = []
    for u in urls:
        if len(docs) >= depth:
            break
        try:
            txt = _trafilatura_text(u)
        except Exception:  # noqa: BLE001
            txt = ""
        if txt and len(txt) >= _MIN_CHARS:
            docs.append({"url": u, "text": txt[:4000]})
    if not docs:
        return None, []
    context = "\n\n".join(f"[{i + 1}] {d['url']}\n{d['text']}" for i, d in enumerate(docs))
    prompt = (
        f"Research brief: {brief}\n" + (f"Focus: {focus}\n" if focus else "") + f"\nSources (numbered):\n{context}\n\n"
        "Write a useful, FACTUAL research note in markdown:\n"
        "## Summary\n(3-5 sentences)\n\n## Key findings\n(4-8 bullets; each cites a source as [n])\n\n"
        "Use ONLY facts present in the sources and cite them by [n]. No fluff, no invented specifics; "
        "if the sources don't answer part of the brief, say so plainly."
    )
    # Route the distillation through the 'coding' profile (owl-alpha -> gpt-oss-120b -> minimax) — factual
    # reasoning over sources beats grok. Uses a DEDICATED profile key 'research-contract' (mapped to 'coding'
    # in llm_providers.json) so ONLY this contract distill uses owl-alpha — the ad-hoc web-researcher crew
    # keeps its own default profile + model unchanged.
    llm = get_llm(for_tool_use=False, temperature=0.3, agent_name="research-contract")
    summary = (llm.call([{"role": "user", "content": prompt}]) or "").strip()
    return (summary or None), [d["url"] for d in docs]


def _advance(oid: str, wid: str, req: dict, **changes) -> None:
    # Drop server metadata (_createdAt/_updatedAt/_version) — a strict-locked schema rejects extra fields.
    rec = {k: v for k, v in {**req, **changes}.items() if not k.startswith("_")}
    if _call(
        "aimeat_workspace_write", {"organism_id": oid, "ws": wid, "space": IN_SPACE, "id": rec["id"], "value": rec}
    ):
        _call("aimeat_workspace_publish", {"organism_id": oid, "ws": wid, "namespace": IN_NS, "id": rec["id"]})


def process_research_requests(max_items: int = 5, targets: list[tuple[str, str]] | None = None) -> dict:
    """Fulfil pending `research-request` records across the agent's member workspaces (or `targets`).

    Deterministic: discover -> claim (in-progress) -> research -> write research-result -> advance (done).
    `targets` = optional list of (organism_id, ws_id) to restrict to (else auto-discover). Returns counts.
    """
    pairs = targets if targets is not None else _member_workspaces()
    if targets is None:  # gate the discovery path on engagements (0.14.0 gates only the push path)
        from crewaimeat.engagements import engaged_pairs

        pairs = engaged_pairs(AGENT, pairs, contract=CONTRACT["id"])
    today = datetime.date.today().isoformat()
    processed = failed = 0
    for oid, wid in pairs:
        if processed + failed >= max_items:
            break
        data = _call("aimeat_workspace_read", {"organism_id": oid, "ws": wid})
        if not data or data.get("manifest") is None:
            continue
        reqs = (data.get("objects", {}) or {}).get(IN_SPACE) or []
        # Output-dedup (the primary guard — it survives restarts): a request whose result already exists is
        # already fulfilled, so just settle it. The result lives in a different space than the request, so
        # this stays reliable even if the request's own status is slow to reflect a write.
        done_results = {r.get("id") for r in ((data.get("objects", {}) or {}).get(OUT_SPACE) or [])}
        for req in reqs:
            rid = req.get("id")
            if req.get("status") != "requested" or not rid:
                continue
            if rid in _PROCESSED:  # already handled this run — guard against a stale 'requested' read
                continue
            if f"res-{rid}" in done_results:  # result already exists -> fulfilled; settle without re-running
                _PROCESSED.add(rid)
                _advance(oid, wid, req, status="done", result_ref=f"res-{rid}")
                continue
            if processed + failed >= max_items:
                break
            _PROCESSED.add(rid)
            _advance(oid, wid, req, status="in-progress")  # CLAIM
            try:
                summary, sources = do_research(req.get("brief", ""), int(req.get("depth") or 5), req.get("focus", ""))
            except Exception as exc:  # noqa: BLE001
                _advance(oid, wid, req, status="failed", error=repr(exc)[:300])
                failed += 1
                print(f"[{AGENT}] research FAILED for {rid}: {exc!r}", file=sys.stderr)
                continue
            if not summary:
                _advance(oid, wid, req, status="failed", error="no usable sources found")
                failed += 1
                continue
            out_id = f"res-{rid}"
            # research-result is a DOCUMENT space → write a markdown note (renders as a page), not a record.
            title = (req.get("brief", "") or "Research note").strip()[:90]
            srcs = "\n".join(f"- {u}" for u in sources)
            md = f"{summary}\n\n## Sources\n{srcs}\n\n*Research brief: {req.get('brief', '')} · requested by {req.get('requested_by', '?')} · {today}*"
            wrote = _call(
                "aimeat_workspace_write",
                {
                    "organism_id": oid,
                    "ws": wid,
                    "space": OUT_SPACE,
                    "id": out_id,
                    "value": {"title": title, "markdown": md},
                },
            )
            pub = (
                _call("aimeat_workspace_publish", {"organism_id": oid, "ws": wid, "namespace": OUT_NS, "id": out_id})
                if wrote
                else None
            )
            if wrote and pub:
                _advance(oid, wid, req, status="done", result_ref=out_id)  # ADVANCE
                processed += 1
            else:
                _advance(oid, wid, req, status="failed", error="result write failed")
                failed += 1
                print(f"[{AGENT}] result write FAILED for {out_id}", file=sys.stderr)
    return {"processed": processed, "failed": failed}


def make_research_contract_tools(agent_name: str) -> list:
    """The single contract-processing tool. It reads + fulfils research-requests; it posts nothing external."""

    @tool("process_research_requests")
    def _process(max_items: int = 5) -> str:
        """Fulfil pending `research-request` records in the workspaces this agent belongs to: claim each,
        research the brief live (web search + fetch + distill), write a `research-result`, and advance the
        request to done. Deterministic; never contacts anyone external. Returns the counts."""
        res = process_research_requests(max_items=max_items)
        return f"research-contract: processed {res['processed']} request(s), {res['failed']} failed."

    return [_process]
