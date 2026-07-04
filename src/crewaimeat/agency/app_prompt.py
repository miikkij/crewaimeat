"""app_prompt — AIMEAT's canonical "Generate App with AI" create-app prompt, for the appliance.

The "build any app I imagine" path does NOT need any crew: it mirrors what aimeat.io/app-catalog.html
does. The appliance fills this VERBATIM prompt with the user's idea + an optional starting template; the
user copies it into any capable AI (Claude/ChatGPT), gets one self-contained HTML file, and publishes it
(via `author_tool.publish_app_html`). When a capable model is configured, the appliance can also run the
prompt itself.

Keep the prompt text a FAITHFUL copy of AIMEAT's (don't invent one) — only the Language line and the
`My initial idea:` line are parameterized, plus an optional one-line template hint. `_LANG_NAME` maps the
appliance UI language to the language the built app should speak.
"""

from __future__ import annotations

_LANG_NAME = {"en": "English", "fi": "Finnish"}

# The starting templates AIMEAT's app-catalog offers (id, title, description, and a one-line steer that is
# appended to the prompt so the interview starts from that shape). id="" = build from scratch (no steer).
TEMPLATES: list[dict] = [
    {"id": "", "title": "(none — build from scratch)", "description": "", "hint": ""},
    {
        "id": "realtime-social",
        "title": "Realtime social room",
        "description": "A live room: logged-in users get live presence + chat with durable history.",
        "hint": "Start from a REALTIME SOCIAL ROOM: logged-in users get live presence + chat with durable "
        "history (realtime rooms + shared public AIMEAT.data).",
    },
    {
        "id": "marketplace",
        "title": "Marketplace (single-seller storefront)",
        "description": "Anyone browses + searches the public listings and opens a detail view.",
        "hint": "Start from a MARKETPLACE (single-seller storefront): anyone browses + searches the public "
        "listings and opens a detail view; the owner manages listings behind login.",
    },
    {
        "id": "homepage",
        "title": "Homepage / personal site",
        "description": "A single-writer public site: anyone views the owner profile + blog/feed.",
        "hint": "Start from a HOMEPAGE / personal site: a single-writer public site — anyone views the owner "
        "profile + blog/feed; only the owner writes.",
    },
    {
        "id": "standard",
        "title": "Standard app — login + saves your data",
        "description": "Login + saves your data.",
        "hint": "Start from a STANDARD app: login + saves the user's own private data (AIMEAT.data private).",
    },
    {
        "id": "data",
        "title": "Data app — built-in tables, forms & charts",
        "description": "Built-in tables, forms & charts.",
        "hint": "Start from a DATA app: built-in tables, forms & charts (use the aimeat-ui-viewers / "
        "aimeat-ui-forms / aimeat-charts bundles).",
    },
    {
        "id": "connected",
        "title": "Connected app — fetches outside data / runs on a schedule (advanced)",
        "description": "Fetches outside data / runs on a schedule (advanced).",
        "hint": "Start from a CONNECTED app (advanced): it fetches outside data / runs on a schedule.",
    },
]


def templates() -> list[dict]:
    """The template menu for the picker (id/title/description; the `hint` steers the prompt, not shown)."""
    return [{"id": t["id"], "title": t["title"], "description": t["description"]} for t in TEMPLATES]


def _template_hint(template_id: str | None) -> str:
    t = next((x for x in TEMPLATES if x["id"] == (template_id or "")), None)
    return (t or {}).get("hint", "")


# The VERBATIM AIMEAT create-app prompt. Only __LANG__ / __IDEA__ / __TEMPLATE_HINT__ are substituted.
_PROMPT = """Language: talk to me and write ALL user-facing text (UI labels, buttons, messages) in __LANG__.
These build instructions are in English, but converse with me and build the app interface in __LANG__.

Help me build a single-file HTML app that runs on AIMEAT.
My initial idea: __IDEA____TEMPLATE_HINT__

## Step 1 — Interview me first
If I have not described my idea above, your FIRST reply must ask me what I want to build. Then ask me these in ONE message and wait for my answers:
1. What kind of app? (message board · multiplayer game · notes/journal · habit or expense tracker · family tools like shared lists/calendar · drawing/creative · music jam · real-time collaboration · offer or need help/services · something else)
2. What should it be called?
3. How should it look and feel? (e.g. dark neon · cozy · sleek minimal · fun colorful) — it must support BOTH light and dark.
4. Data: SHARED (a community space others can see and add to) or PRIVATE (only mine)?
5. Should it use AI features (summaries, suggestions, generation)? If yes I can enable them via aimeat-ai.
Skip any question I already answered in my idea above. Use my answers to customise everything in Step 2.

## Step 2 — Build it (once I have answered)

This app runs in the AIMEAT ecosystem. Here is what you need to know:

### Available Client Libraries
Load with <script src> from the node base https://aimeat.io/v1/libs/. Include ONLY the ones you use. Load aimeat-auth first — the others build on its session.

Core:
- aimeat-auth.js — login button, JWT, session (`AIMEAT.auth`, `session.fetch()`)
- aimeat-data.js — private/public key-value memory + search (`AIMEAT.data`)
- aimeat-storage.js — file upload/download (`AIMEAT.storage`)
- aimeat-organism.js — organisms & workspaces: list, normalized workspace read (published + drafts merged per item), write drafts, publish, README, search (`AIMEAT.organism`). Requires aimeat-auth.

AI (prompt-driven — see the AI section below):
- aimeat-ai.js — LLM completions on the USER's own OpenRouter key (`AIMEAT.ai.complete`). Requires aimeat-auth.

Social & economy:
- aimeat-social.js — boards, posts, reactions (`AIMEAT.social`)
- aimeat-wallet.js — morsel balance + transactions (`AIMEAT.wallet`)
- aimeat-work.js — actions / work requests (`AIMEAT.work`)
- aimeat-agents.js — commission & watch the owner's AI agents (`AIMEAT.agents`)
- aimeat-capabilities.js — discover & invoke shared capabilities (`AIMEAT.capabilities`)

Media & misc:
- aimeat-audio.js — audio engine: instruments, synth, soundboard
- aimeat-speech.js — text-to-speech / speech helpers
- aimeat-markdown.js — render markdown INTO an element: `AIMEAT.md.render(text, target)` (returns an Element — never assign it to innerHTML; use `renderToString(text)` for a string). `await AIMEAT.md.renderRich(text, target)` adds task lists, footnotes, code highlighting, Mermaid diagrams AND live data embeds: a ```aimeat-memory fence (lines `key: <memory key>`, optional `view: table|props|list|value|json`, `fields: a,b`, `title: …`) renders that memory key as a fresh table on every open — perfect for agent-produced data in documents.
- aimeat-editor.js — markdown editor: `AIMEAT.editor.mount(el, {value, onChange})`, `AIMEAT.editor.toolbar(adapter)`, `AIMEAT.editor.split(el, {value, onChange})` for editor + live preview (pairs with aimeat-markdown.js)
- aimeat-header.js — drop-in canonical site header (nav + theme)
- aimeat-tunnel.js — personal-node tunnel client (advanced)

Ready-made UI (node-bundled — load from https://aimeat.io/v1/cortex/<name>/libs/<name>.js, use only what you need):
- aimeat-ui-viewers — sortable/filterable DataTable + viewers (`AIMEAT.ui.viewers`)
- aimeat-ui-forms — form builder with validation (`AIMEAT.ui.forms`)
- aimeat-ui-layout — responsive layout helpers, master/detail (`AIMEAT.ui.layout`)
- aimeat-ui-nav — navbars, tabs, menus (`AIMEAT.ui.nav`)
- aimeat-ui-dialogs — modals, toasts, confirms (`AIMEAT.ui.dialogs`)
- aimeat-charts — charts / graphs (`AIMEAT.charts`)
- aimeat-canvas — drawing / freeform canvas (`AIMEAT.canvas`)
Example: <script src="https://aimeat.io/v1/cortex/aimeat-ui-viewers/libs/aimeat-ui-viewers.js"></script>

### Auth Pattern
Handle BOTH login paths: a fresh sign-in click (the onLogin callback) AND a page that loads already signed in (restore the session yourself). `onLogin` fires ONLY on a fresh sign-in — it does NOT fire on reload when a session already exists, so a page that relies on onLogin alone shows nothing to an already-logged-in returning user.
```html
<script src="https://aimeat.io/v1/libs/aimeat-auth.js"></script>
<script>
function showApp(session) { /* session.owner, session.jwt, session.fetch() */ }
function hideApp() { /* hide content, show a "Sign in" message */ }

// Path 1 — fresh sign-in / sign-out via the login button:
AIMEAT.auth.mountLoginButton("#login", {
  onLogin: showApp,   // fires ONLY on a fresh sign-in click, NOT on reload
  onLogout: hideApp
});

// Path 2 — already signed in when the page loads. Restore the stored session
// explicitly; login() returns the session (or null if not signed in).
AIMEAT.auth.login().then(function (session) { if (session) showApp(session); });
</script>
```

### Data Storage
Match the PRIVATE vs SHARED choice from Step 1:
```javascript
// PRIVATE — scoped to the logged-in owner, only they can read it:
await AIMEAT.data.set("myapp.notes", data, { visibility: "private", tags: ["myapp"] });
const mine = await AIMEAT.data.get("myapp.notes");
// SHARED/community — public so everyone can read; each user writes their own key:
await AIMEAT.data.set("myapp.shared.<unique-id>", entry, { visibility: "public" });
const theirs = await AIMEAT.data.getPublic(ownerGaii, "myapp.shared.<id>");  // read others
const results = await AIMEAT.data.search("query");
```
Works only when logged in. After a write, read it back to confirm it persisted.

### AI (prompt-driven)
aimeat-ai runs an LLM on the LOGGED-IN USER's own OpenRouter key — free for the app, and the user controls spend. Load aimeat-auth first, then gate every "Use AI" control on isAvailable().
```html
<script src="https://aimeat.io/v1/libs/aimeat-auth.js"></script>
<script src="https://aimeat.io/v1/libs/aimeat-ai.js"></script>
```
```javascript
if (await AIMEAT.ai.isAvailable()) {            // false until login + key configured
  const r = await AIMEAT.ai.complete({ app_id: "my-app", prompt: "Summarise:\\n" + text });
  render(r.content);                            // also: r.model, r.usage, r.budget
} else { showHint("Log in and add an AI key to enable this."); }
// Structured output: const { parsed } = await AIMEAT.ai.completeJson({ app_id, prompt, schema });
```
Always handle isAvailable()===false and catch errors; never hardcode an API key in the app.

### Real-time / multiplayer (optional)
For shared live state (presence boards, 1v1 games) use realtime rooms via your authenticated session.fetch:
```javascript
// 1) create or join a room
const room = (await session.fetch("/v1/realtime/rooms", { method: "POST",
  body: JSON.stringify({ name: "my-room" }) })).data;   // → { id, ws_url }
// 2) open a WebSocket for live presence + messages
const ws = new WebSocket(location.origin.replace(/^http/, "ws") + room.ws_url);
ws.onmessage = (e) => handle(JSON.parse(e.data));
// 3) for low-latency P2P, GET /v1/realtime/ice-servers and use WebRTC
```
Simpler apps can skip rooms and just observe shared AIMEAT.data keys on a timer.

### Design Guidelines
Use CSS variables so the app themes cleanly, and RESPECT the user's AIMEAT theme: the light/dark choice they made in the AIMEAT pill is saved in localStorage "aimeat-theme" ("light"|"dark"). Define light as the default and dark under [data-theme="dark"], then set that attribute from the saved choice on load (fall back to the OS preference, and live-update if it changes):
```css
:root { --bg:#fafaf8; --card:#fff; --text:#1a1a2e; --accent:#e8564a; --border:#e5e7eb; --radius:12px; }
:root[data-theme="dark"] { --bg:#14141c; --card:#1e1e2a; --text:#ececf4; --border:#2e2e40; }
```
```js
(function(){ function apply(t){ document.documentElement.setAttribute("data-theme", t==="dark"?"dark":"light"); }
  apply(localStorage.getItem("aimeat-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  addEventListener("storage", function(e){ if(e.key==="aimeat-theme" && e.newValue) apply(e.newValue); }); })();
```
Always include <meta name="viewport" content="width=device-width, initial-scale=1.0">. Mobile-first, single self-contained HTML file with embedded CSS + JS.

### Important Rules
- Return the COMPLETE HTML file, not fragments
- Never use literal closing script tags in JS comments or strings
- Keep it as a single self-contained HTML file
- Load only the libraries you actually use; load aimeat-auth before libs that need a session
- Gate AI features on AIMEAT.ai.isAvailable() and handle the logged-out / no-key case
- Theme with CSS variables; respect the user's AIMEAT light/dark choice (localStorage "aimeat-theme") with an OS-preference fallback
- Include error handling and loading states for API calls

## When the app is ready — tell me how to publish it
After you hand me the finished single HTML file, END your reply by telling me (in my language) that I can publish it straight from the aimeat-agency app: open the "Generate App with AI" panel, paste the HTML into the "Add & publish your app" box, and it goes live on my own AIMEAT node with a shareable link (it keeps working with my AIMEAT login, saved data, files, AI and realtime features)."""


def build_prompt(idea: str = "", template_id: str | None = None, lang: str = "en") -> str:
    """Fill the verbatim AIMEAT create-app prompt with the user's idea + optional template + language."""
    lang_name = _LANG_NAME.get(lang, "English")
    idea = (idea or "").strip() or "(I haven't described it yet — ask me in Step 1.)"
    hint = _template_hint(template_id)
    hint_block = f"\n\n{hint}" if hint else ""
    return _PROMPT.replace("__LANG__", lang_name).replace("__IDEA__", idea).replace("__TEMPLATE_HINT__", hint_block)
