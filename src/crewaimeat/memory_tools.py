"""Memory tools — let a CONTENT crew read/write the owner's memory at specific keys with a chosen
visibility. The enabling toolkit for the news pipeline: a fetcher writes raw material, a writer reads
raw + writes own-words articles, an editorial agent reads articles + writes the editorial — each to
PUBLIC dated keys so an anonymous newspaper app can read them.

Why this exists: the scaffold's default deliverable-publish writes ONE derived key at `owner` visibility
(it does not let an agent target arbitrary keys or set public visibility). Content agents need exactly
that, so they get explicit tools. Backed by `_aimeat_call` (the connector's CLI path) — verified that
aimeat_memory_write/read/list ARE CLI-callable (unlike the schedule tools, which are MCP-only).

Usage (in a crew's build_domain — crew-forge wires this for content/writer/editorial crews):
    from crewaimeat.memory_tools import make_memory_tools
    mem_tools = make_memory_tools(AGENT_NAME)
    agent = Agent(..., tools=[*mem_tools], llm=ctx.llm)
"""

from __future__ import annotations

import json

from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call


def owner_scope_value(agent_name: str, key: str):
    """The value a SIBLING agent wrote at an exact key. Cross-agent read: memory is namespaced by the
    WRITING agent's GAII, so a value written by a sibling (e.g. the fetcher's raw keys) is NOT under
    this agent's own GAII. owner_scope=true lists across ALL same-owner agents (the pattern
    workflow.py uses to collect workers' deliverables)."""
    r = _aimeat_call(agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": key})
    items = (r or {}).get("items") if isinstance(r, dict) else None
    for it in items or []:
        if isinstance(it, dict) and it.get("key") == key and it.get("value") is not None:
            return it.get("value")
    return None


def read_owner_key(agent_name: str, key: str):
    """The value at an EXACT owner memory key, including one written by a SIBLING agent.

    Own GAII first, then the same-owner cross-agent lookup — the path the news pipeline already
    proved. Returns None when the key has no value anywhere. Use this when you do not know which
    agent wrote the key; use `owner_scope_value` when you already know it was a sibling.
    """
    # owner_scope=True IS THE READ, not an optimisation. Without it `aimeat_memory_read` looks only
    # in the CALLER's namespace (`<agent>#<owner>@<node>`), so an agent cannot see anything a person
    # or an app wrote under the owner's own GHII. Measured 2026-08-25: julkaisu-tutkija got
    # NOT_FOUND on julkaisu.2026-08-25.tilaus one minute after the app had written it, then
    # researched the words of its task description instead — which is how it ended up reading about
    # WhatsApp and PDF manuals. The same flag on the same key returns the order in full.
    r = _aimeat_call(agent_name, "aimeat_memory_read", {"key": key, "owner_scope": True})
    val = (r.get("value") if isinstance(r, dict) else r) if r is not None else None
    if val is not None:
        return val
    return owner_scope_value(agent_name, key)


def make_memory_tools(agent_name: str) -> list:
    """Return content-memory crewai tools (write_memory / read_memory / list_memory) for this agent."""

    @tool("write_memory")
    def write_memory(key: str, value: str, visibility: str = "public") -> str:
        """Write a value to the owner's memory at an EXACT key. Use this to persist your deliverable to
        the agreed key (e.g. 'news.2026-06-03.morning.article.talous'). visibility:
          'public' = anyone can read it WITHOUT logging in (use for articles + editorials a public app shows),
          'owner'  = only the owner / same-owner agents can read it (use for raw material if it shouldn't be public).
        `value` is stored as given (write your finished article/editorial text here, or a JSON string for
        structured data). Returns OK or the error. Write each category to its OWN key."""
        vis = (visibility or "public").strip().lower()
        if vis not in ("public", "owner"):
            return "FAILED: visibility must be 'public' or 'owner'."
        if not key or not str(key).strip():
            return "FAILED: key is required (the EXACT memory key to write)."
        # Accept a JSON string (store the parsed object) or plain text (store as-is).
        val: object = value
        sv = value.strip() if isinstance(value, str) else value
        if isinstance(sv, str) and sv[:1] in ("{", "["):
            try:
                val = json.loads(sv)
            except Exception:  # noqa: BLE001 — not JSON, store as plain text
                val = value
        r = _aimeat_call(agent_name, "aimeat_memory_write", {"key": key, "value": val, "visibility": vis})
        if r is None:
            return f"FAILED to write '{key}' (no result from memory_write)."
        return f"OK: wrote '{key}' (visibility={vis})."

    @tool("read_memory")
    def read_memory(key: str) -> str:
        """Read the value at an EXACT owner memory key — INCLUDING keys written by OTHER same-owner agents
        (e.g. a writer reading the fetcher's raw keys). Tries your own memory first, then a same-owner
        cross-agent (owner-scope) lookup. Returns the value, or a clear 'not found' if the key isn't
        written yet — in that case do NOT fabricate content; report the missing upstream key and stop
        (the upstream stage may not have run yet)."""
        if not key or not str(key).strip():
            return "FAILED: key is required."
        val = read_owner_key(agent_name, key)  # own GAII, then same-owner cross-agent (sibling keys)
        if val is None:
            return f"NOT FOUND: '{key}' has no value yet (upstream stage may not have run). Do not fabricate — stop."
        out = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        return f"value of '{key}':\n{out[:8000]}"

    @tool("list_memory")
    def list_memory(prefix: str) -> str:
        """List the owner memory keys under a prefix (e.g. 'news.2026-06-03.' to see what's been written
        for today) ACROSS all same-owner agents (so you see siblings' keys too). Returns each key WITH the
        GAII of the agent that owns it AND its visibility — `- <key> | gaii=<owner_gaii> | <visibility>`.
        The GAII matters: it is what a public viewer / getPublic(gaii, key) needs to read a sibling's key,
        and what you put in a front-page index entry. If the SAME key appears under several GAIIs (e.g. a
        category written by one agent and copied by another), they are SEPARATE entries — pick the one
        from the agent that actually produced the content. Use to discover what exists before reading."""
        r = _aimeat_call(agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": prefix or ""})
        items = ((r or {}).get("items") if isinstance(r, dict) else None) or []
        rows = []
        for it in items:
            if isinstance(it, dict) and it.get("key"):
                g = it.get("owner_gaii") or it.get("gaii") or "?"
                vis = it.get("visibility") or "?"
                rows.append(f"- {it['key']} | gaii={g} | {vis}")
        if not rows:
            return f"No memory keys found under prefix '{prefix}'."
        return f"keys under '{prefix}' (key | gaii | visibility):\n" + "\n".join(rows[:80])

    @tool("index_frontpage")
    def index_frontpage(entries_json: str, index_key: str = "newspaper.frontpage") -> str:
        """Maintain the PUBLIC front-page INDEX that a public viewer app reads — the single key whose value
        lists every item with its own {gaii, key, title, date, summary} so the viewer can fan out to bodies
        that live under MANY different author agents WITHOUT knowing each one up front. Call this as the
        LAST pipeline step (the editorial / publisher stage). It READ-MODIFY-WRITEs the index UNDER YOUR
        OWN GAII at visibility='public' — that GAII is the PUBLISHER the viewer app is pointed at.

        entries_json = a JSON array of items to add/refresh. Each item:
          {"gaii":"<the GAII that owns the BODY key — from list_memory's gaii=… for that article>",
           "key":"<the EXACT public memory key of the body>", "title":"<headline>",
           "date":"<YYYY-MM-DD>", "summary":"<one-line teaser>", "edition":"<morning|evening|…>",
           "category":"<talous|tiede|…>"}
        Use the ARTICLE-AUTHOR's gaii for each article (the writer agent), and YOUR OWN gaii for the
        editorial you just wrote. EVERY body the index points at MUST be visibility='public' (write
        articles/editorials public) or the viewer cannot read it anonymously. Entries are deduped by
        (date, edition, category) — a re-run REPLACES the stale entry for that slot — then sorted
        newest-first and capped. Returns OK + the item count + the PUBLISHER gaii + INDEX_KEY to hand the
        viewer app."""
        try:
            new = json.loads(entries_json) if isinstance(entries_json, str) else entries_json
        except Exception as e:  # noqa: BLE001
            return f"FAILED: entries_json is not valid JSON: {e}"
        if not isinstance(new, list) or not new:
            return "FAILED: entries_json must be a non-empty JSON array of {gaii,key,title,date,summary,…}."
        clean = []
        for it in new:
            if isinstance(it, dict) and it.get("gaii") and it.get("key"):
                clean.append(
                    {
                        k: it.get(k)
                        for k in (
                            "gaii",
                            "key",
                            "title",
                            "date",
                            "summary",
                            "edition",
                            "category",
                            "kind",
                            "sources",
                            "source_urls",  # count AND addresses — an allowlist that drops
                            # source_urls would silently strip the evidence back out of a hand-built row
                        )
                        if it.get(k) is not None
                    }
                )
        if not clean:
            return (
                "FAILED: each entry needs at least gaii + key (the body's owner GAII + its exact public "
                "key, both from list_memory). Nothing had both."
            )
        # Read the current index (own GAII). _aimeat_call returns None for an unset key — start empty.
        cur = _aimeat_call(agent_name, "aimeat_memory_read", {"key": index_key})
        curval = cur.get("value") if isinstance(cur, dict) else None
        existing = curval if isinstance(curval, list) else []

        def _slot(e: dict):
            # Dedup by the body's CONCRETE identity (gaii, key). The pipeline's body keys are deterministic
            # per (date, edition, category) — news.<date>.<edition>.article.<category> — so a re-run of the
            # editorial overwrites each entry IN PLACE and the same body can never be indexed twice. (The old
            # logical-slot key drifted when 'kind'/'category' varied between runs → that was the tuplauutiset
            # bug: a second editorial run appended a second copy of every article.)
            return ("bykey", e.get("gaii"), e.get("key"))

        merged: dict = {}
        for e in existing:
            if isinstance(e, dict) and e.get("gaii") and e.get("key"):
                merged[_slot(e)] = e
        for e in clean:  # incoming overrides any stale entry for the same slot
            merged[_slot(e)] = e
        out = sorted(
            merged.values(), key=lambda e: (str(e.get("date") or ""), str(e.get("edition") or "")), reverse=True
        )[:80]
        w = _aimeat_call(agent_name, "aimeat_memory_write", {"key": index_key, "value": out, "visibility": "public"})
        if w is None:
            return f"FAILED to write the front-page index '{index_key}' (no result from memory_write)."
        # Discover our own GAII (the PUBLISHER) from the just-written key's owner_gaii.
        pub = ""
        lr = _aimeat_call(agent_name, "aimeat_memory_list", {"prefix": index_key})
        for it in ((lr or {}).get("items") if isinstance(lr, dict) else None) or []:
            if isinstance(it, dict) and it.get("key") == index_key and it.get("owner_gaii"):
                pub = it["owner_gaii"]
                break
        pub_txt = pub or "<your own GAII — list_memory the index key to read its gaii=…>"
        return (
            f"OK: front-page index '{index_key}' now lists {len(out)} item(s), visibility=public. "
            f"PUBLISHER (point the viewer app's `const PUBLISHER` at THIS) = {pub_txt}. "
            f"INDEX_KEY = '{index_key}'. The viewer reads this index, then getPublic(item.gaii, item.key) "
            "for each body."
        )

    @tool("index_frontpage_auto")
    def index_frontpage_auto(date: str, edition: str, index_key: str = "newspaper.frontpage") -> str:
        """Build the PUBLIC front-page index for a date+edition DETERMINISTICALLY — no hand-built JSON, so it
        cannot miss an article or miscount. Discovers every news.<date>.<edition>.article.<category> (+ the
        .editorial) key with its owner gaii, reads each body for a title+teaser, and COUNTS each news
        article's web sources from the edition raw (news.<date>.<edition>.raw) so the viewer can show
        'Pohjana N verkkolähdettä'. Read-modify-writes the index under YOUR OWN gaii at visibility='public'.
        Prefer this over index_frontpage in the editorial/publisher stage. Args: date (YYYY-MM-DD), edition
        (morning|evening). Returns OK + item count + PUBLISHER gaii + INDEX_KEY."""
        date, edition = (date or "").strip(), (edition or "").strip()
        if not date or not edition:
            return "FAILED: date (YYYY-MM-DD) and edition (morning|evening) are required."
        apref = f"news.{date}.{edition}.article."
        epref = f"news.{date}.{edition}.editorial"
        lr = _aimeat_call(agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": f"news.{date}.{edition}."})
        rows = ((lr or {}).get("items") if isinstance(lr, dict) else None) or []
        raw_counts: dict = {}
        raw_urls: dict = {}

        def _record_raw(cat_k: str, items) -> None:
            """Tally one category's sources and keep their ADDRESSES.

            A paper that reports a source COUNT it cannot substantiate is making the weakest version
            of the claim it exists to make — and the URLs are right here in the raw we are already
            reading. Only what is actually recorded: never reconstruct or guess an address (dedup,
            keep order)."""
            if isinstance(items, str) and items.strip()[:1] == "[":
                try:
                    items = json.loads(items)
                except ValueError:
                    items = None
            lst = items if isinstance(items, list) else []
            raw_counts[cat_k] = len(lst)
            raw_urls[cat_k] = list(dict.fromkeys(str(a["url"]) for a in lst if isinstance(a, dict) and a.get("url")))

        for it in rows:
            k = it.get("key") or ""
            # Both raw shapes: the consolidated `…raw` record (every category under `categories`) and
            # the pre-consolidation `…raw.<category>` keys, so an already-published edition still
            # rebuilds its front page with the same source counts it shipped with.
            if k == f"news.{date}.{edition}.raw":
                v = it.get("value")
                if v is None:
                    v = owner_scope_value(agent_name, k)
                if isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except ValueError:
                        v = None
                for cat_k, items in (((v or {}).get("categories") or {}) if isinstance(v, dict) else {}).items():
                    _record_raw(cat_k, items)
            elif k.startswith(f"news.{date}.{edition}.raw."):
                v = it.get("value")
                if v is None:
                    v = owner_scope_value(agent_name, k)
                _record_raw(k.rsplit(".", 1)[-1], v)
        FEATURE = {"koodaus", "prompt-niksi", "matikka"}
        clean = []
        for it in rows:
            k = it.get("key") or ""
            is_ed = k == epref
            if not (k.startswith(apref) or is_ed):
                continue
            cat = "editorial" if is_ed else k.rsplit(".", 1)[-1]
            body = it.get("value")
            if body is None:
                body = owner_scope_value(agent_name, k)
            txt = body if isinstance(body, str) else (json.dumps(body, ensure_ascii=False) if body is not None else "")
            lines = [ln.strip().lstrip("#").strip() for ln in txt.splitlines() if ln.strip()]
            entry = {
                "gaii": it.get("owner_gaii") or it.get("gaii"),
                "key": k,
                "date": date,
                "edition": edition,
                "category": cat,
                "summary": (" ".join(lines))[:140],
                "kind": "editorial" if is_ed else "article",
                "title": (f"Editorial | {date} {edition}" if is_ed else f"{cat.capitalize()} | {date}"),
            }
            if (not is_ed) and (cat not in FEATURE) and (cat in raw_counts):
                entry["sources"] = raw_counts[cat]  # the COUNT stays — app + agent face both read it
                if raw_urls.get(cat):
                    entry["source_urls"] = raw_urls[cat]  # ...and now the addresses behind the count
            clean.append(entry)
        if not clean:
            return f"FAILED: no article/editorial keys found for {date} {edition}."
        cur = _aimeat_call(agent_name, "aimeat_memory_read", {"key": index_key})
        existing = cur.get("value") if isinstance(cur, dict) else None
        merged: dict = {}
        for e in existing if isinstance(existing, list) else []:
            if isinstance(e, dict) and e.get("gaii") and e.get("key"):
                merged[(e.get("gaii"), e.get("key"))] = e
        for e in clean:
            merged[(e.get("gaii"), e.get("key"))] = e
        out = sorted(
            merged.values(), key=lambda e: (str(e.get("date") or ""), str(e.get("edition") or "")), reverse=True
        )[:120]
        w = _aimeat_call(agent_name, "aimeat_memory_write", {"key": index_key, "value": out, "visibility": "public"})
        if w is None:
            return f"FAILED to write the front-page index '{index_key}'."
        pub = ""
        for it in ((_aimeat_call(agent_name, "aimeat_memory_list", {"prefix": index_key}) or {}).get("items")) or []:
            if isinstance(it, dict) and it.get("key") == index_key and it.get("owner_gaii"):
                pub = it["owner_gaii"]
                break
        ev = sum(1 for e in out if e.get("date") == date and e.get("edition") == edition)
        return (
            f"OK: front-page index '{index_key}' rebuilt for {date} {edition} — {ev} items this edition, "
            f"{len(out)} total, visibility=public. PUBLISHER = {pub or '<list_memory the index key>'}. "
            f"INDEX_KEY = '{index_key}'."
        )

    tools = [write_memory, read_memory, list_memory, index_frontpage, index_frontpage_auto]
    for _t in tools:  # side-effecting / live-state — never serve a cached result
        try:
            _t.cache_function = lambda *_a, **_k: False
        except Exception:  # noqa: BLE001
            pass
    return tools
