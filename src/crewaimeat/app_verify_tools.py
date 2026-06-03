"""Crew tool wrapping the deterministic app quality gates (app_verify) so a conductor agent can run
them on a built app and get a PASS/FAIL verdict with exact errors. Crew-side; no AIMEAT changes.

The conductor calls `verify_app(project_id)` AFTER a build; on FAIL it routes a fix and re-verifies.
This is the deterministic "catch" that the autonomous path lacked — the gates run as CODE, not LLM
judgement, so a broken app never passes as 'done'.
"""
from __future__ import annotations

import json

import requests
from crewai.tools import tool

from crewaimeat import app_verify
from crewaimeat.generator_tool import _call, _discover_owner, _node_base, _token


def make_verify_tools(agent_name: str, owner: str | None = None) -> list:
    owner = owner or _discover_owner(agent_name)

    @tool("verify_app")
    def verify_app(project_id: str) -> str:
        """Run the deterministic quality gates on a generated AIMEAT app and return PASS or a FAIL
        report listing the exact problems. Gates: (1) every cortex lib parses (no SyntaxError);
        (2) cortex memory reads use the service_slug prefix; (3) the seeded memory keys exist;
        (4) the published app loads headless with no console errors + real content. Call this after a
        build BEFORE declaring done. If it returns FAIL, route the named fix to aimeat-cortex-fixer
        (or re-build) and call verify_app again — do not complete a project that fails this."""
        g = _call(agent_name, owner, "GET", f"/v1/generator/{project_id}")
        if not (isinstance(g, dict) and g.get("ok")):
            return f"VERIFY ERROR: cannot read project {project_id}: {g.get('_status')}"
        node_id = g.get("node") or "aimeat-finland-001-genesis"
        d = g.get("data") or {}
        proj = d.get("project") or {}
        bp = proj.get("blueprint") or {}
        if isinstance(bp, str):
            try:
                bp = json.loads(bp)
            except Exception:  # noqa: BLE001
                bp = {}
        slug = bp.get("service_slug") or ""
        # Only check keys actually STORED as memory values — i.e. produced by a memory/translation
        # component. Keys produced by csm (schemas) or extensions are not runtime memory values, so
        # checking them would false-flag (e.g. csm's `fleet.schema` is not a `<slug>.fleet.schema` value).
        mk = (bp.get("dataModel") or {}).get("memoryKeys") or {}
        id_type = {(c or {}).get("id"): (c or {}).get("type") for c in (bp.get("components") or [])}
        memkeys = [k for k, v in mk.items()
                   if id_type.get((v or {}).get("producedBy")) in ("memory", "translation")]
        prefixes = sorted({str(k).split(".")[0] for k in memkeys})
        comps = d.get("components") or []
        if isinstance(comps, dict):
            comps = list(comps.values())
        base = (_node_base(agent_name, owner) or "").rstrip("/")
        tok, _ = _token(agent_name, owner)
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        problems: list[str] = []

        # Gate 3 — seeded memory keys exist (the "AIMEAT.data.get gets the i18n" check)
        if slug and memkeys:
            dp = app_verify.app_data_present(agent_name, owner, slug, memkeys, node_id)
            if not dp["ok"]:
                missing = {k: v for k, v in dp["keys"].items() if "MISSING" in v}
                problems.append(f"seeded data missing: {missing}")

        # Gates 1+2 — each served cortex lib parses AND reads slug-prefixed keys
        for c in comps:
            c = c or {}
            if c.get("type") != "cortex":
                continue
            name = c.get("registeredAs")
            if not name:
                problems.append(f"cortex {c.get('id')} not registered (no registeredAs)")
                continue
            try:
                js = requests.get(f"{base}/v1/cortex/{name}/libs/{name}.js", headers=headers, timeout=20).text
            except Exception as e:  # noqa: BLE001
                problems.append(f"{name}: lib fetch failed ({e!r})")
                continue
            ok_s, err_s = app_verify.cortex_syntax_ok(js)
            if not ok_s:
                problems.append(f"{name}: SYNTAX {err_s}")
            if slug and prefixes:
                ok_p, off = app_verify.cortex_uses_slug(js, slug, prefixes)
                if not ok_p:
                    problems.append(f"{name}: reads un-slug-prefixed keys {off} (use '{slug}.<key>')")

        # Gate 4 — published app loads headless without console errors + has content
        appfn = next((c.get("registeredAs") for c in comps if (c or {}).get("type") == "app"), None)
        if appfn and base:
            url = f"{base}/v1/apps/{owner}/{appfn}?mode=inline"
            r = app_verify.app_renders(url, rewrite_node=("aimeat-local-001-dev", node_id))
            if r.get("ok") is False:
                problems.append(f"render: console_errors={r.get('console_errors')} | "
                                f"sample={(r.get('content_sample') or '')[:140]!r}")
            elif r.get("ok") is None:
                problems.append(f"render: not run ({r.get('skipped')})")

        if problems:
            return "VERIFY FAIL — fix these, then re-verify:\n- " + "\n- ".join(problems)
        return (f"VERIFY PASS — all gates green. slug={slug}, app={appfn}, cortexes parse + read "
                f"slug-prefixed keys, seeded data present, app renders with no console errors.")

    return [verify_app]
