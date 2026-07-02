"""cockpit — the aimeat-agency local control server (Slice 1, step 3).

A small FastAPI app the desktop appliance's Tauri shell spawns on a free port and points its webview at.
It is the ONLY thing the UI talks to, and it just wires together what already exists:

  - brains / brain_templates  -> the gallery + brain CRUD + versioning/rollback + instantiate
  - fleet_state.build_snapshot -> the fleet read model (offline by default; node read is opt-in)
  - tui.actions                -> start / stop / restart a crew (the same safe controls the TUI uses)
  - local_memory               -> the memory browser + the Sync view (raw local vs published upward)

Security (a locked architecture decision): the server BINDS TO 127.0.0.1 ONLY and requires a per-launch
bearer token on every `/api/*` request — the cockpit can start/stop crews and publish memory, so any
other local process must not be able to drive it. The Tauri shell mints the token (env
`AIMEAT_AGENCY_TOKEN`) before spawning and passes it to the webview; in standalone/dev runs the server
generates one and prints it. `/healthz` is open (no secrets) so the shell can poll for readiness.

Run standalone (dev):  uv run python -m crewaimeat.agency.cockpit
"""

from __future__ import annotations

import dataclasses
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from crewaimeat import brain_templates, brains, local_memory
from crewaimeat.agency import account, events

COCKPIT_VERSION = "0.8.30"
_TOKEN_ENV = "AIMEAT_AGENCY_TOKEN"
_STATIC = Path(__file__).parent / "static"
# Default local model for the wizard. gemma4 is capable enough for the agentic onboarding + news task
# (proven); small 3B models (llama3.2:3b) were too weak in practice. Small-GPU users can pick a lighter
# model in the picker or use OpenRouter.
DEFAULT_OLLAMA_MODEL = "gemma4"


# ── request bodies ────────────────────────────────────────────────────────────
class BrainIn(BaseModel):
    agent_name: str
    template_id: str
    prose: str | None = None
    policy: dict | None = None
    title: str | None = None


class BrainEdit(BaseModel):
    prose: str | None = None
    policy: dict | None = None
    title: str | None = None


class RollbackIn(BaseModel):
    version: int


class PublishIn(BaseModel):
    id: str
    key: str
    visibility: str = "owner"


class ConnectIn(BaseModel):
    owner: str
    node: str | None = None


class TestIn(BaseModel):
    prompt: str


class KeyIn(BaseModel):
    key: str


class PullIn(BaseModel):
    model: str = DEFAULT_OLLAMA_MODEL


class UrlIn(BaseModel):
    url: str


def _brain_diff(prev: dict | None, new: dict) -> list[str]:
    """What changed between two brain versions — for the activity log ('created', 'prose',
    'policy.autonomy', …) so History shows not just THAT a save happened but WHAT it changed."""
    if not prev:
        return ["created"]
    changed = []
    if (prev.get("prose") or "") != (new.get("prose") or ""):
        changed.append("prose")
    pp, np_ = prev.get("policy") or {}, new.get("policy") or {}
    for k in sorted(set(pp) | set(np_)):
        if pp.get(k) != np_.get(k):
            changed.append("policy." + k)
    return changed or ["no change"]


def _require_token_dependency(app: FastAPI):
    """A dependency that enforces the per-launch bearer token (constant-time compare). Accepts the token
    in the Authorization header OR a `?token=` query param — the latter is for EventSource (SSE), which
    can't set headers."""

    def _check(authorization: str | None = Header(default=None), token: str | None = Query(default=None)) -> None:
        expected = app.state.token
        got = ""
        if authorization and authorization.lower().startswith("bearer "):
            got = authorization[7:].strip()
        elif token:
            got = token.strip()
        if not (expected and got and secrets.compare_digest(got, expected)):
            raise HTTPException(status_code=401, detail="missing or invalid agency token")

    return _check


def _ollama_base() -> str:
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


def _ollama_probe() -> tuple[bool, list[str]]:
    """(running, model-names). `running` = Ollama answered; the model list may be empty (not yet pulled)."""
    import requests

    try:
        r = requests.get(f"{_ollama_base()}/api/tags", timeout=2)
        if r.status_code != 200:
            return False, []
        tags = r.json().get("models") or []
        return True, [n for m in tags if (n := (m.get("name") or m.get("model")))]
    except Exception:  # noqa: BLE001 — Ollama not running / unreachable is normal
        return False, []


def _ollama_bin() -> str | None:
    """The ollama executable, or None when not installed. Checks PATH first, then the Windows
    user-scope default install dir — a JUST-installed ollama is on the user PATH only after a
    re-login, so the wizard's install->start flow needs the direct path."""
    import shutil

    p = shutil.which("ollama")
    if p:
        return p
    if os.name == "nt":
        cand = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
        if os.path.isfile(cand):
            return cand
    return None


def _ollama_pidfile() -> str:
    from crewaimeat._home import aimeat_home

    return os.path.join(aimeat_home(), "agency_ollama.pid")


def _pid_is_ollama(pid: int) -> bool:
    """Is `pid` actually an ollama process RIGHT NOW? Windows reuses pids aggressively, so a stale
    pidfile (reboot, dead spawn) could otherwise make shutdown taskkill an innocent process tree."""
    import subprocess

    try:
        if os.name == "nt":
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10
            )
            return "ollama" in (r.stdout or "").lower()
        r = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True, timeout=10)
        return "ollama" in (r.stdout or "").lower()
    except Exception:  # noqa: BLE001 — can't verify -> treat as NOT ollama (never kill blind)
        return False


def _start_agency_ollama() -> dict:
    """Start `ollama serve` as an AGENCY-OWNED child when it is installed but not running (the fresh-
    install first session, or autostart disabled/killed). The pid is recorded so shutdown stops exactly
    what WE started — a user-started ollama (no pidfile) is never touched. Pytest-guarded: a test must
    never spawn a real server."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return {"started": False, "reason": "pytest"}
    running, _ = _ollama_probe()
    if running:
        return {"started": False, "reason": "already running"}
    exe = _ollama_bin()
    if not exe:
        return {"started": False, "reason": "not installed", "download": "https://ollama.com/download"}
    import subprocess

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen([exe, "serve"], creationflags=creationflags, close_fds=True)
    with open(_ollama_pidfile(), "w", encoding="utf-8") as f:
        f.write(str(proc.pid))
    return {"started": True, "pid": proc.pid}


def _stop_agency_ollama() -> str:
    """Stop the ollama server ONLY if the agency started it (the pidfile we wrote, AND the pid is still
    an ollama process — a reused pid after a reboot must never be killed). A user's own/autostart ollama
    has no pidfile and is left running. Best-effort."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return "agency-ollama stop skipped (pytest)"
    path = _ollama_pidfile()
    if not os.path.isfile(path):
        return "ollama not agency-started (left running)"
    import subprocess

    try:
        pid = int(open(path, encoding="utf-8").read().strip())
        if not _pid_is_ollama(pid):
            os.remove(path)  # stale pidfile (reboot / dead spawn) — clean it, kill nothing
            return f"stale agency-ollama pidfile removed (pid {pid} is not ollama)"
        if os.name == "nt":
            # /T also takes the model runner children (llama-server) that hold the GPU memory
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
        os.remove(path)
        return f"stopped agency-started ollama (pid {pid})"
    except Exception as exc:  # noqa: BLE001 — shutdown hygiene is best-effort
        return f"agency-ollama stop failed ({type(exc).__name__})"


def _ollama_models() -> list[dict]:
    """Local models from a RUNNING Ollama, as picker entries with an ollama override spec. Empty if Ollama
    isn't up. llm.py routes ollama/<model> to the local endpoint and skips forced tool-use — so a local
    model is a first-class, free, private choice."""
    base = _ollama_base()
    _running, names = _ollama_probe()
    return [
        {
            "label": f"ollama:{n}",
            "id": n,
            "context": None,
            "local": True,
            "spec": {
                "kind": "model",
                "label": f"ollama:{n}",
                "provider": {"type": "ollama", "name": "ollama", "base_url": base, "models": [{"id": n}]},
            },
        }
        for n in names
    ]


def _has_openrouter_key() -> bool:
    if os.environ.get("OPENROUTER_API_KEY"):
        return True
    try:
        from crewaimeat.forge import _project_root

        env = _project_root() / ".env"
        if not env.is_file():
            return False
        for ln in env.read_text(encoding="utf-8").splitlines():
            # a blank `OPENROUTER_API_KEY=` line is NOT a saved key — don't tell the wizard it is
            if ln.startswith("OPENROUTER_API_KEY=") and ln.split("=", 1)[1].strip():
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _set_openrouter_key(key: str) -> None:
    """Persist OPENROUTER_API_KEY to the runtime's .env (the fleet load_dotenv's it) + this process."""
    from crewaimeat.forge import _project_root

    env = _project_root() / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.is_file() else []
    lines = [ln for ln in lines if not ln.startswith("OPENROUTER_API_KEY=")]
    lines.append(f"OPENROUTER_API_KEY={key}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["OPENROUTER_API_KEY"] = key


# The wizard drives one model download at a time; this is its outcome so a failed pull (no network,
# disk full, ollama not on PATH yet) SURFACES in /api/setup/status instead of spinning forever.
_PULL_STATE: dict = {"running": False, "model": None, "error": None}

# `npm install -g aimeat` progress for the wizard's engine step — same shape, same reason.
_ENGINE_INSTALL: dict = {"running": False, "error": None}


def _ver_tuple(v: str | None) -> tuple[int, ...]:
    import re

    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def _latest_agency_release() -> tuple[str | None, str | None]:
    """(latest agency version, release html_url) from GitHub, or (None, None). Best-effort, short timeout."""
    import requests

    try:
        r = requests.get(
            "https://api.github.com/repos/miikkij/crewaimeat/releases",
            timeout=4,
            headers={"Accept": "application/vnd.github+json"},
        )
        if r.status_code != 200:
            return None, None
        best, url = None, None
        for rel in r.json():
            tag = rel.get("tag_name") or ""
            if not tag.startswith("agency-v"):
                continue
            v = tag[len("agency-v") :]
            if best is None or _ver_tuple(v) > _ver_tuple(best):
                best, url = v, rel.get("html_url")
        return best, url
    except Exception:  # noqa: BLE001 — offline / rate-limited is fine
        return None, None


def _task_brief(tt: dict) -> dict:
    """Normalize a node task to what the UI shows: id/title/status + input + start/finish times."""
    return {
        "id": tt.get("id") or tt.get("task_id"),
        "title": tt.get("title"),
        "status": tt.get("status"),
        "description": (tt.get("description") or "")[:500],
        "created": tt.get("createdAt") or tt.get("created_at") or tt.get("created"),
        "completed": tt.get("completedAt") or tt.get("completed_at") or tt.get("updatedAt"),
    }


def _agent_live_state(agent: str) -> dict:
    """A small live snapshot for the SSE stream: the agent's run status + its task queue (id/title/
    status). Pure read; the SSE loop diffs successive snapshots and only pushes on change."""
    from crewaimeat.aimeat_crew import _aimeat_call
    from crewaimeat.tui import fleet_state

    status = "down"
    for row in fleet_state.build_snapshot(node_index={}).rows:
        if row.agent == agent:
            status = row.status
            break
    r = _aimeat_call(agent, "aimeat_task_list", {})
    tasks = []
    if isinstance(r, dict):
        raw = r.get("tasks") or (r.get("data") or {}).get("tasks") or []
        tasks = [_task_brief(tt) for tt in (raw if isinstance(raw, list) else []) if isinstance(tt, dict)]
    return {"status": status, "tasks": tasks, "attached": r is not None}


def create_app(token: str | None = None) -> FastAPI:
    """Build the cockpit FastAPI app. `token` (or env AIMEAT_AGENCY_TOKEN, or a fresh random one) gates
    every `/api/*` route. Returned for both `main()` and tests (via Starlette's TestClient)."""
    app = FastAPI(title="aimeat-agency cockpit", version=COCKPIT_VERSION)
    app.state.token = token or os.environ.get(_TOKEN_ENV) or secrets.token_urlsafe(32)
    account.apply_env()  # export a previously-saved owner as AIMEAT_OWNER for this process
    try:
        for old, new in brains.migrate_invalid_names():  # self-heal e.g. 'Mapmaker' -> 'mapmaker' so connect works
            print(f"[cockpit] renamed agent '{old}' -> '{new}' (connector requires a lowercase slug)")
    except Exception:  # noqa: BLE001 — never block startup on the migration
        pass
    require_token = Depends(_require_token_dependency(app))

    @app.get("/healthz")
    def healthz() -> dict:  # open: liveness only, no secrets — the shell polls this for readiness
        return {"ok": True, "service": "aimeat-agency-cockpit", "version": COCKPIT_VERSION}

    @app.get("/", response_class=HTMLResponse)
    def index(boot: str | None = Query(default=None)) -> str:
        # Serve the single-file UI with the per-launch token injected — but ONLY to a caller that
        # already knows the token (`?boot=`). Without this gate, any local process could GET / and
        # read the injected token out of the HTML, making the whole /api/* token gate decorative.
        # The Tauri shell appends ?boot= when it navigates; standalone main() prints the full URL.
        if not (boot and app.state.token and secrets.compare_digest(boot.strip(), app.state.token)):
            raise HTTPException(
                status_code=401,
                detail="missing or invalid boot token — open the cockpit via the app window "
                "(or the ?boot= URL printed at startup)",
            )
        try:
            html = (_STATIC / "index.html").read_text(encoding="utf-8")
        except OSError:
            return "<h1>aimeat-agency cockpit</h1><p>UI asset missing.</p>"
        return html.replace("__AGENCY_TOKEN__", app.state.token)

    # ── account / identity (where the agents live + does the app have access) ───
    @app.get("/api/account", dependencies=[require_token])
    def get_account() -> dict:
        """The identity this operator's agents live under: owner @ home node, and whether any agent is
        already approved (connected). `owner_set` is False on a fresh install — the cue for the first-run
        Connect screen. This is what the header shows so 'where does my agent go?' is always answered."""
        from crewaimeat.tui import fleet_state

        acc = account.load()
        sagents = fleet_state.collect_serve().get("agents") or []
        return {**acc, "connected": bool(sagents), "registered_agents": len(sagents)}

    @app.post("/api/account/connect", dependencies=[require_token])
    def connect_account(body: ConnectIn) -> dict:
        """Set the owner + home node new agents register under. This does NOT grant access by itself —
        access is proven per-agent when the owner approves each one via device-auth (see register).

        NOTE: the guided-setup endpoints (/api/setup/status, /api/ollama/pull, /api/setup/openrouter-key)
        are defined below; the onboarding wizard drives them in order."""
        try:
            return account.save(body.owner, body.node)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/setup/status", dependencies=[require_token])
    def setup_status() -> dict:
        """Aggregate state for the onboarding wizard: account, model (local Ollama / OpenRouter key), and
        the first agent's progress (created → connected → running) — so the wizard shows the whole checklist
        and gates each step on the previous."""
        from crewaimeat.tui import fleet_state

        acc = account.load()
        running, names = _ollama_probe()
        _fam = DEFAULT_OLLAMA_MODEL.split(":")[0]  # e.g. "gemma4" — present in any variant tag (gemma4:latest)
        has_model = any(_fam in n for n in names)
        # The embed model powers the opt-in crew memory (CrewSpec.memory / pipeline_memory) — the
        # cascade's preferred free+private tier. Surfaced so the wizard can show/gate it.
        embed_model = os.getenv("AIMEAT_EMBED_OLLAMA_MODEL", "nomic-embed-text")
        has_embed = any(n == embed_model or n.startswith(embed_model + ":") for n in names)
        bs = brains.list_brains()
        first = bs[0]["agent_name"] if bs else None
        first_auth = account.agent_auth(first, acc["owner"]) if first else {"authorized": False}
        serve_agents = {a.get("agent") for a in (fleet_state.collect_serve().get("agents") or [])}
        from crewaimeat import node_engine

        return {
            "owner_set": acc["owner_set"],
            "owner": acc["owner"],
            "node": acc["node"],
            # Node.js + the AIMEAT connector CLI — the engine agents run on. Every dev box had both on
            # PATH, so nothing checked; a fresh machine has neither and device-auth/serve just died.
            "engine": {
                **node_engine.engine_status(),
                "install_running": _ENGINE_INSTALL["running"],
                "install_error": _ENGINE_INSTALL["error"],
            },
            "ollama": {
                "installed": _ollama_bin() is not None,
                "running": running,
                "has_model": has_model,
                "default_model": DEFAULT_OLLAMA_MODEL,
                "embed_model": embed_model,
                "has_embed_model": has_embed,
                "models": names,
                "pull_running": _PULL_STATE["running"],
                "pull_error": _PULL_STATE["error"],
            },
            "openrouter_key": _has_openrouter_key(),
            "brain_count": len(bs),
            "first_agent": first,
            "first_agent_connected": bool(first_auth.get("authorized")),
            "first_agent_running": bool(first and first in serve_agents),
        }

    @app.post("/api/ollama/start", dependencies=[require_token])
    def ollama_start() -> dict:
        """Start ollama as an agency-owned child (installed but not running — the fresh-install first
        session, or autostart off). The wizard polls /api/setup/status until `running`. Shutdown stops
        it again because we own the pid; a user-started ollama is never ours to stop."""
        return _start_agency_ollama()

    @app.post("/api/engine/install", dependencies=[require_token])
    def engine_install() -> dict:
        """Install the AIMEAT connector CLI (`npm install -g aimeat@<pinned>`) in the background — the
        wizard's engine step drives this once Node.js is present, and polls /api/setup/status until
        `engine.connector_cli` flips (or `engine.install_error` says why it won't). Node.js itself is a
        download-and-run installer we only guide to (like Ollama) — we never install system software."""
        if os.environ.get("PYTEST_CURRENT_TEST"):  # a test must never npm-install into the real machine
            return {"started": False, "reason": "pytest"}
        from crewaimeat import node_engine
        from crewaimeat.forge import AIMEAT_CONNECTOR

        if node_engine.aimeat_cli():
            return {"started": False, "reason": "already installed"}
        if _ENGINE_INSTALL["running"]:
            return {"started": False, "reason": "already installing"}
        npm = node_engine.npm_bin()
        if not npm:
            raise HTTPException(status_code=400, detail="Node.js is not installed yet — install it first")
        import subprocess
        import threading

        _ENGINE_INSTALL.update(running=True, error=None)
        argv = ["cmd", "/c", npm] if os.name == "nt" else [npm]  # npm is a .cmd shim on Windows

        def _install():
            try:
                r = subprocess.run(
                    [*argv, "install", "-g", AIMEAT_CONNECTOR], capture_output=True, text=True, timeout=600
                )
                if r.returncode != 0:
                    tail = ((r.stderr or r.stdout or "").strip().splitlines() or ["(no output)"])[-1]
                    _ENGINE_INSTALL["error"] = f"npm install failed: {tail[:300]}"
            except Exception as exc:  # noqa: BLE001 — surfaced via the status poll
                _ENGINE_INSTALL["error"] = f"npm install failed: {type(exc).__name__}: {exc}"
            finally:
                _ENGINE_INSTALL["running"] = False

        threading.Thread(target=_install, daemon=True).start()
        return {"started": True, "package": AIMEAT_CONNECTOR}

    @app.post("/api/ollama/pull", dependencies=[require_token])
    def ollama_pull(body: PullIn) -> dict:
        """Kick off `ollama pull <model>` (default gemma4) in the background; the wizard polls
        /api/setup/status until the model appears (or `pull_error` reports why it won't).

        Uses the RESOLVED ollama path (_ollama_bin) — a just-installed ollama is on the user PATH only
        after a re-login, so the bare "ollama" that worked on every dev box failed the exact first-run
        flow the wizard exists for, silently. Failures land in _PULL_STATE for the status endpoint.

        Also pulls the EMBED model (nomic-embed-text, ~274 MB) right after — it is the embedder
        cascade's preferred free+private tier, so opt-in crew memory (CrewSpec.memory /
        pipeline_memory) works on the appliance out of the box instead of failing its prerequisite."""
        if os.environ.get("PYTEST_CURRENT_TEST"):  # a test must never download real models
            return {"started": False, "reason": "pytest"}
        if _PULL_STATE["running"]:
            return {"started": False, "reason": "already pulling", "model": _PULL_STATE["model"]}
        import subprocess
        import threading

        model = (body.model or DEFAULT_OLLAMA_MODEL).strip()
        embed_model = os.getenv("AIMEAT_EMBED_OLLAMA_MODEL", "nomic-embed-text")
        exe = _ollama_bin()
        if not exe:
            _PULL_STATE.update(running=False, model=model, error="Ollama is not installed (executable not found)")
            return {"started": False, "reason": "not installed", "download": "https://ollama.com/download"}
        _PULL_STATE.update(running=True, model=model, error=None)

        def _pull():
            try:
                for m in (model, embed_model):
                    r = subprocess.run([exe, "pull", m], capture_output=True, text=True, timeout=3600)
                    if r.returncode != 0:
                        tail = ((r.stderr or r.stdout or "").strip().splitlines() or ["(no output)"])[-1]
                        _PULL_STATE["error"] = f"ollama pull {m} failed: {tail[:300]}"
                        return  # the chat model failed -> don't mask it with an embed-pull attempt
            except Exception as exc:  # noqa: BLE001 — surfaced via the status poll, never a silent spin
                _PULL_STATE["error"] = f"ollama pull failed: {type(exc).__name__}: {exc}"
            finally:
                _PULL_STATE["running"] = False

        threading.Thread(target=_pull, daemon=True).start()
        return {"started": True, "model": model, "embed_model": embed_model}

    @app.post("/api/setup/openrouter-key", dependencies=[require_token])
    def set_openrouter_key_route(body: KeyIn) -> dict:
        """Store an OpenRouter API key (the cloud-model 'advanced' option) in the runtime's .env."""
        if not body.key or not body.key.strip():
            raise HTTPException(status_code=400, detail="key is required")
        _set_openrouter_key(body.key.strip())
        return {"ok": True}

    @app.post("/api/open", dependencies=[require_token])
    def open_external(body: UrlIn) -> dict:
        """Open a URL in the operator's DEFAULT browser. The cockpit is a local process, so this works in
        the Tauri shell too (its webview swallows window.open). Only http(s) is allowed."""
        import webbrowser

        url = (body.url or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(status_code=400, detail="only http(s) URLs are allowed")
        try:
            webbrowser.open(url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"could not open browser: {e}") from e
        return {"ok": True}

    @app.get("/api/update-check", dependencies=[require_token])
    def update_check() -> dict:
        """Is a newer aimeat-agency published? Compares this build to the latest GitHub agency-v* release.
        The UI shows a banner with a Download link (opened via /api/open) when one is available."""
        latest, url = _latest_agency_release()
        available = bool(latest and _ver_tuple(latest) > _ver_tuple(COCKPIT_VERSION))
        return {
            "current": COCKPIT_VERSION,
            "latest": latest,
            "update_available": available,
            "url": url or "https://github.com/miikkij/crewaimeat/releases",
        }

    def _unload_ollama_models() -> str:
        """Best-effort `ollama stop <model>` for every loaded model at appliance shutdown — releases the
        GPU/driver-backed memory (10+ GB with a chat + embed model resident) immediately instead of
        waiting out the keep-alive. ONLY when the agency started the ollama server (our pidfile): a
        machine-wide ollama shared with another fleet (the dev box!) keeps its warm models — unloading
        them would cold-start every neighbouring crew's next call. Never blocks the shutdown."""
        if os.environ.get("PYTEST_CURRENT_TEST"):  # a test must never unload the dev box's live models
            return "ollama unload skipped (pytest)"
        if not os.path.isfile(_ollama_pidfile()):
            return "ollama models left warm (server not agency-started)"
        import subprocess

        try:
            import requests

            exe = _ollama_bin()  # resolved path — bare "ollama" isn't on PATH on a fresh install
            if not exe:
                return "ollama unload skipped (executable not found)"
            base = (os.getenv("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
            models = [m.get("name") for m in (requests.get(f"{base}/api/ps", timeout=3).json().get("models") or [])]
            for m in filter(None, models):
                subprocess.run([exe, "stop", m], capture_output=True, timeout=15)
            return f"unloaded {len(models)} ollama model(s)" if models else "no ollama models loaded"
        except Exception as exc:  # noqa: BLE001 — shutdown hygiene is best-effort; keep-alive unloads anyway
            return f"ollama unload skipped ({type(exc).__name__})"

    @app.post("/api/shutdown", dependencies=[require_token])
    def shutdown() -> dict:
        """Stop THIS install's fleet (crews + serve, repo/home-scoped — never another fleet), unload the
        ollama models the fleet had loaded (frees the GPU-backed memory right away), then, when launched
        by the Tauri shell, self-exit so the shell can quit the app. The UI shows 'safely stopped' from
        the returned detail before the window closes."""
        import threading
        import time

        from crewaimeat.tui import actions

        detail = actions.stop_fleet()
        detail += " | " + _unload_ollama_models()  # free the GPU-backed model memory right away
        detail += " | " + _stop_agency_ollama()  # and stop the server too, IF the agency started it
        events.record("_agency", "shutdown", {"detail": detail[:200]})
        if os.environ.get(_TOKEN_ENV):  # shell-launched -> exit so the shell's child-watcher quits the app

            def _bye() -> None:
                time.sleep(1.0)
                os._exit(0)

            threading.Thread(target=_bye, daemon=True).start()
        return {"ok": True, "detail": detail}

    @app.post("/api/reset", dependencies=[require_token])
    def reset() -> dict:
        """Wipe ALL agency state (account, brains, agents, memory, tokens) for a true fresh start — what the
        uninstaller's 'delete application data' doesn't reach. Stops the fleet first so nothing is locked."""
        import shutil

        from crewaimeat._home import aimeat_home
        from crewaimeat.tui import actions

        try:
            actions.stop_fleet()
        except Exception:  # noqa: BLE001
            pass
        removed = []
        # Brains live in a SQLite DB that may be LOCKED (so file-unlink can fail silently) — clear the rows
        # through the data layer instead, plus its model overrides. This is the bit a plain file delete missed.
        try:
            from crewaimeat import llm

            for b in brains.list_brains():
                name = b["agent_name"]
                if brains.delete_brain(name):
                    removed.append("brain:" + name)
                llm.clear_override(name)
        except Exception:  # noqa: BLE001
            pass
        _stop_agency_ollama()  # if WE started ollama, stop it too — reset means a truly cold start
        home = Path(aimeat_home())
        for name in (
            "brains.db",
            "local_memory.db",
            "events.db",
            "agency_account.json",
            "llm_overrides.json",
            "serve.json",
            "agency_ollama.pid",  # else a later shutdown could act on a stale (reused) pid
        ):
            for suffix in ("", "-wal", "-shm"):
                p = home / (name + suffix)
                try:
                    if p.exists():
                        p.unlink()
                        removed.append(p.name)
                except OSError:
                    pass
        tok = home / "tokens"
        if tok.is_dir():
            shutil.rmtree(tok, ignore_errors=True)
            removed.append("tokens/")
        for f in Path("crews").glob("*_crew.py"):  # generated brain stubs
            try:
                f.unlink()
                removed.append(f.name)
            except OSError:
                pass
        # The reset confirm promises ALL settings go — that includes the saved OpenRouter key (.env)
        # and this process's copy of it, so the wizard's model step truly starts over.
        try:
            from crewaimeat.forge import _project_root

            envf = _project_root() / ".env"
            if envf.is_file():
                lines = [
                    ln
                    for ln in envf.read_text(encoding="utf-8").splitlines()
                    if not ln.startswith("OPENROUTER_API_KEY=")
                ]
                envf.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
                removed.append(".env:OPENROUTER_API_KEY")
        except OSError:
            pass
        os.environ.pop("OPENROUTER_API_KEY", None)
        # Crew/register logs are agent data too (prompts, outputs, device codes) — a fresh start drops them.
        logs = Path("logs")
        if logs.is_dir():
            shutil.rmtree(logs, ignore_errors=True)  # best-effort: a file held open just survives
            removed.append("logs/")
        os.environ.pop("AIMEAT_OWNER", None)  # so the wizard restarts at step 1
        return {"ok": True, "removed": removed}

    @app.post("/api/agents/{agent}/register", dependencies=[require_token])
    def register_agent_route(agent: str) -> dict:
        """Begin device-auth for an agent: launch the connector's OAuth device flow and surface the
        verification CODE + URL the owner enters in their aimeat.io dashboard (Profile → Agents). The
        agent registers automatically once approved — poll auth-status to detect it. Nothing runs against
        the account until that approval, so the app can never act without the owner's explicit consent."""
        import re as _re

        from crewaimeat import forge, node_engine

        acc = account.load()
        if not acc["owner"]:
            raise HTTPException(status_code=400, detail="connect an owner first (POST /api/account/connect)")
        if not node_engine.npx_bin():  # device-auth shells out to npx — fail with the fix, not WinError 2
            raise HTTPException(
                status_code=400,
                detail="Node.js is required to connect an agent but was not found — "
                "finish the 'agent engine' step in Setup first.",
            )
        try:
            ok, msg = forge.register_agent(agent, acc["owner"], acc["node"])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"could not start device-auth: {e}") from e
        m = _re.search(r"open (\S+) and enter code (\S+)", msg or "")
        verify_url = m.group(1) if m else None
        code = m.group(2) if m else None
        # The verify page needs the code IN the URL (it errors on a bare /verify) — build the deep link.
        verify_link = None
        if verify_url and code:
            verify_link = verify_url + ("&" if "?" in verify_url else "?") + "code=" + code
        events.record(agent, "connect_requested", {"code": code})
        return {
            "agent": agent,
            "ok": ok,
            "message": msg,
            "verify_url": verify_url,
            "verify_link": verify_link,
            "code": code,
        }

    @app.get("/api/agents/{agent}/auth-status", dependencies=[require_token])
    def agent_auth_status(agent: str) -> dict:
        """Has the owner approved this agent and does its token work? The gate before running it."""
        res = account.agent_auth(agent, account.load()["owner"])
        if res["authorized"] and not events.has_kind(agent, "connected"):
            events.record(agent, "connected")  # log the moment approval first takes effect (once)
        return res

    @app.get("/api/models", dependencies=[require_token])
    def list_models() -> dict:
        """The model catalogue for the policy picker (from llm_providers.json). Each carries a `spec` to
        store as the brain's policy.model override; selecting one and RESTARTING the agent applies it."""
        from crewaimeat import llm

        models = _ollama_models()  # local first — free + private; only present if Ollama is running
        seen = {m["label"] for m in models}  # the live-detected local models win over any config copy
        for m in llm.available_models():
            if m["label"] in seen:
                continue
            seen.add(m["label"])
            models.append(
                {
                    "label": m["label"],
                    "id": m["id"],
                    "context": m.get("context"),
                    "local": False,
                    "spec": {"kind": "model", "label": m["label"], "provider": m["provider"]},
                }
            )
        return {"models": models}

    # ── templates (the gallery) ────────────────────────────────────────────────
    @app.get("/api/templates", dependencies=[require_token])
    def list_templates(lang: str = "en") -> dict:
        # localized so a Finnish operator sees the template — and the prose they start editing — in fi.
        return {"templates": [t.localized(lang) for t in brain_templates.all_templates()]}

    # ── brains (CRUD + versioning) ─────────────────────────────────────────────
    @app.get("/api/brains", dependencies=[require_token])
    def list_brains() -> dict:
        return {"brains": brains.list_brains()}

    @app.post("/api/brains", dependencies=[require_token])
    def create_brain(body: BrainIn) -> dict:
        # The agent name becomes the connector identity, which must be 3-64 lowercase alphanumeric + hyphens
        # (the connector rejects e.g. 'Mapmaker' and device-auth then fails). Slug it at the boundary.
        name = brains.slug_agent_name(body.agent_name)
        if len(name) < 3:
            raise HTTPException(
                status_code=400, detail="agent name must be 3–64 lowercase letters, numbers, or hyphens"
            )
        try:
            prev = brains.get_brain(name)
            saved = brains.save_brain(name, body.template_id, prose=body.prose, policy=body.policy, title=body.title)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        events.record(
            saved["agent_name"], "brain_saved", {"version": saved["version"], "changed": _brain_diff(prev, saved)}
        )
        return saved

    @app.get("/api/brains/{agent}", dependencies=[require_token])
    def get_brain(agent: str) -> dict:
        b = brains.get_brain(agent)
        if b is None:
            raise HTTPException(status_code=404, detail=f"no brain '{agent}'")
        return b

    @app.patch("/api/brains/{agent}", dependencies=[require_token])
    def edit_brain(agent: str, body: BrainEdit) -> dict:
        b = brains.get_brain(agent)
        if b is None:
            raise HTTPException(status_code=404, detail=f"no brain '{agent}'")
        # keep the same template; save_brain falls back to existing prose/policy when a field is omitted
        saved = brains.save_brain(agent, b["template_id"], prose=body.prose, policy=body.policy, title=body.title)
        events.record(agent, "brain_saved", {"version": saved["version"], "changed": _brain_diff(b, saved)})
        return saved

    @app.delete("/api/brains/{agent}", dependencies=[require_token])
    def delete_brain(agent: str) -> dict:
        # Stop the crew first so we don't orphan a running daemon, then drop the brain + its model override.
        from crewaimeat import llm
        from crewaimeat.tui import actions

        try:
            actions.stop_crew(agent)
        except Exception:  # noqa: BLE001 — not running / already stopped is fine
            pass
        deleted = brains.delete_brain(agent)
        try:
            llm.clear_override(agent)
        except Exception:  # noqa: BLE001
            pass
        # Remove the connector TOKEN(s) too — otherwise the serve daemon keeps loading a deleted agent from
        # its leftover token file (the 'news-paska is still served though I deleted it' zombie).
        try:
            import glob

            from crewaimeat._home import aimeat_home

            for tokf in glob.glob(str(aimeat_home() / "tokens" / f"{agent}@*.token")):
                try:
                    os.remove(tokf)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass
        return {"deleted": deleted}

    @app.get("/api/brains/{agent}/history", dependencies=[require_token])
    def brain_history(agent: str) -> dict:
        return {"versions": brains.history(agent)}

    @app.post("/api/brains/{agent}/rollback", dependencies=[require_token])
    def rollback_brain(agent: str, body: RollbackIn) -> dict:
        try:
            restored = brains.rollback(agent, body.version)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        events.record(agent, "rolled_back", {"to_version": body.version, "version": restored["version"]})
        return restored

    @app.get("/api/agents/{agent}/activity", dependencies=[require_token])
    def agent_activity(agent: str) -> dict:
        """The full timeline: cockpit events (saved brain / connect / start…) MERGED with the agent's
        actual TASK RUNS from the node — each with its input (title+description), status, and a result the
        UI can open. So History shows not just what the operator did but what the agent actually ran."""
        import datetime as _dt

        from crewaimeat.aimeat_crew import _aimeat_call

        def _epoch(iso):
            if not iso:
                return None
            try:
                return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                return None

        evs = list(events.activity(agent))
        tl = _aimeat_call(agent, "aimeat_task_list", {})
        for tt in (tl.get("tasks") or []) if isinstance(tl, dict) else []:
            if not isinstance(tt, dict):
                continue
            evs.append(
                {
                    "ts": _epoch(tt.get("completedAt") or tt.get("updatedAt") or tt.get("createdAt")),
                    "kind": "task",
                    "detail": {
                        "id": tt.get("id"),
                        "title": tt.get("title"),
                        "description": (tt.get("description") or "")[:300],
                        "status": tt.get("status"),
                    },
                }
            )
        evs.sort(key=lambda e: e.get("ts") or 0, reverse=True)
        return {"events": evs}

    # ── tasks: the agent's queue — what's queued / running / done, and a live test-run ─────────
    @app.get("/api/agents/{agent}/tasks", dependencies=[require_token])
    def agent_tasks(agent: str) -> dict:
        """The agent's own task queue from the node: queued / active / done / failed. Read-only — this is
        the 'is it processing something, is there a backlog?' view."""
        from crewaimeat.aimeat_crew import _aimeat_call

        r = _aimeat_call(agent, "aimeat_task_list", {})
        if r is None:  # the connector swallowed an error (commonly: agent not loaded in the serve daemon)
            return {"tasks": [], "error": "agent_not_attached"}
        raw = r.get("tasks") or (r.get("data") or {}).get("tasks") or (r if isinstance(r, list) else [])
        return {"tasks": [_task_brief(tt) for tt in (raw if isinstance(raw, list) else []) if isinstance(tt, dict)]}

    @app.post("/api/agents/{agent}/test", dependencies=[require_token])
    def test_run(agent: str, body: TestIn) -> dict:
        """Fire a REAL one-off task at the (running) agent so the operator can see what it actually does —
        the agent's own daemon picks it up on its real model. Returns the task id to poll for the result.
        Needs the agent approved + running."""
        from crewaimeat.aimeat_crew import _aimeat_call
        from crewaimeat.tui.test_run import _find_id

        if brains.get_brain(agent) is None:
            raise HTTPException(status_code=404, detail=f"no brain '{agent}'")
        resp = _aimeat_call(
            agent,
            "aimeat_task_create",
            {
                "target_agent": agent,
                "title": f"Test: {body.prompt[:48]}",
                "description": body.prompt,
                "status": "queued",
            },
        )
        tid = _find_id(resp)
        if not tid:
            raise HTTPException(
                status_code=502,
                detail="the agent's connection isn't up: either the connector doesn't have this agent "
                "attached yet (it loads agents at startup — press Start/Restart so it's picked up), or "
                "the connection to your node is down (check your internet). Then try again.",
            )
        events.record(agent, "test_run", {"task_id": tid, "prompt": body.prompt[:120]})
        return {"ok": True, "task_id": tid}

    @app.get("/api/agents/{agent}/stream", dependencies=[require_token])
    def agent_stream(agent: str) -> StreamingResponse:
        """Server-Sent Events: push the agent's run-status + task-queue to the browser whenever they
        CHANGE — so the Manage page updates live with no browser polling. (The cockpit watches state
        server-side; the browser only receives pushes.)"""
        import json as _json
        import time as _time

        def gen():
            last = None
            while True:
                try:
                    state = _agent_live_state(agent)
                except Exception:  # noqa: BLE001
                    state = {"status": "unknown", "tasks": [], "attached": False}
                payload = _json.dumps(state)
                if payload != last:
                    last = payload
                    yield f"data: {payload}\n\n"
                else:
                    yield ": ping\n\n"  # keep-alive; no state change
                _time.sleep(4)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/agents/{agent}/task/{tid}/result", dependencies=[require_token])
    def task_result(agent: str, tid: str) -> dict:
        """Poll a task's outcome. Keys off the TASK STATUS (done/failed) — not just a deliverable hunt —
        so a finished-but-empty task stops the spinner with the truth instead of spinning forever. Reads
        the result from the task's own deliverableKey first, then falls back to the memory pattern."""
        from crewaimeat.aimeat_crew import _aimeat_call
        from crewaimeat.tui.test_run import _read_deliverable

        g = _aimeat_call(agent, "aimeat_task_get", {"task_id": tid})
        task = g.get("task") if isinstance(g, dict) else None
        status = (task or {}).get("status")
        rkey = (task or {}).get("deliverableKey")
        result = None
        if rkey:
            r = _aimeat_call(agent, "aimeat_memory_read", {"key": rkey})
            result = (r.get("value") if isinstance(r, dict) else r) if r else None
        if result is None:  # older completes didn't set deliverableKey — find it by the task's short id
            rkey, result = _read_deliverable(_aimeat_call, agent, tid.split("-", 1)[0])
        terminal = status in ("done", "failed")
        return {
            "task_id": tid,
            "status": status,
            "done": bool(terminal or result is not None),
            "key": rkey,
            "result": (str(result) if result is not None else None),
        }

    # ── offering: advertise the agent's capability on the node (the free, outbound half) ───────
    @app.get("/api/agents/{agent}/offer", dependencies=[require_token])
    def get_offer(agent: str) -> dict:
        """What this agent can advertise (from its template) and whether the operator has opted in."""
        b = brains.get_brain(agent)
        if b is None:
            raise HTTPException(status_code=404, detail=f"no brain '{agent}'")
        tmpl = brain_templates.get(b["template_id"])
        meta = tmpl.offer if tmpl else None
        return {"available": meta is not None, "offer": meta, "enabled": bool((b["policy"] or {}).get("offer_enabled"))}

    @app.post("/api/agents/{agent}/offer/publish", dependencies=[require_token])
    def publish_offer(agent: str) -> dict:
        """ADVERTISE this agent's capability on the node so others can discover + request it (opt-in,
        explicit — no automatic leak outward). Needs the agent approved. The one-click order + escrow
        path is the node side (Slice 0/2); this is the free advertising half."""
        from crewaimeat import offers

        b = brains.get_brain(agent)
        if b is None:
            raise HTTPException(status_code=404, detail=f"no brain '{agent}'")
        tmpl = brain_templates.get(b["template_id"])
        meta = tmpl.offer if tmpl else None
        if not meta:
            raise HTTPException(status_code=400, detail="this template advertises no offer")
        ok, detail = offers.publish_meta_offer(agent, meta)
        if not ok:
            raise HTTPException(status_code=502, detail=detail)
        pol = dict(b["policy"] or {})
        pol["offer_enabled"] = True
        brains.save_brain(agent, b["template_id"], policy=pol)
        events.record(agent, "offer_published", {"offer_id": meta["id"]})
        return {"ok": True, "offer_id": meta["id"]}

    @app.post("/api/brains/{agent}/instantiate", dependencies=[require_token])
    def instantiate_brain(agent: str) -> dict:
        if brains.get_brain(agent) is None:
            raise HTTPException(status_code=404, detail=f"no brain '{agent}'")
        return {"stub": brains.write_crew_stub(agent)}

    @app.get("/api/brains/{agent}/dry-run", dependencies=[require_token])
    def dry_run(agent: str) -> dict:
        """A node-independent PLAN PREVIEW: build the crew from the brain and report what it WOULD run —
        the roster (roles + tools) and each task's resolved description. (The full PROPOSE phase, with a
        real spend estimate, runs against the node once the agent is live — that is a later step.)"""
        from crewaimeat.aimeat_crew import BuildContext

        b = brains.get_brain(agent)
        if b is None:
            raise HTTPException(status_code=404, detail=f"no brain '{agent}'")
        from crewaimeat import llm

        ov = llm.agent_override(agent) or {}
        model = ov.get("model") or "(routed by llm_providers.json)"
        spec = brains.build_crewspec(agent)
        ctx = BuildContext(task={}, prompt="", llm=str(model), today="(current time injected at run)")
        agents, tasks = spec.build_domain(ctx)
        return {
            "agent": agent,
            "template_id": b["template_id"],
            "model": model,
            "agents": [{"role": a.role, "goal": a.goal, "tools": [t.name for t in a.tools]} for a in agents],
            "tasks": [{"description": t.description, "expected_output": t.expected_output} for t in tasks],
            "note": "plan preview (no LLM, no spend). Full PROPOSE runs live once the agent is started.",
        }

    # ── fleet (read + controls) ────────────────────────────────────────────────
    @app.get("/api/fleet", dependencies=[require_token])
    def fleet(node: int = 0) -> dict:
        from crewaimeat.tui import fleet_state

        # node=0 (default) reads ONLY local state (process table + locks + serve.json) — no network, no
        # daemon spawn. node=1 also makes the one read-only aimeat_agents_list call for last_seen/mode.
        snap = fleet_state.build_snapshot(node_index=None if node else {})
        # The cockpit manages BRAIN agents only. The appliance bundle ships the repo's example crews/
        # too, so an unfiltered roster showed a non-dev ~40 unfamiliar dev agents — hide everything the
        # operator didn't create here. (The dev fleet's full view is the TUI, not the cockpit.)
        mine = {b["agent_name"] for b in brains.list_brains()}
        snap.rows = [r for r in snap.rows if r.agent in mine]
        snap.zombies = [r.agent for r in snap.rows if r.status == "zombie"]
        return dataclasses.asdict(snap)

    @app.get("/api/fleet/{agent}/logs", dependencies=[require_token])
    def fleet_logs(agent: str, n: int = 80) -> dict:
        """Tail the agent's log so its activity (dry-run, runs, errors) is visible on its own page —
        same files the TUI reads. Returns the last `n` lines, or empty when the agent hasn't run yet."""
        candidates = [
            f"{agent}.watchdog.log",
            f"{agent.replace('-', '_')}_crew.watchdog.log",
            f"{agent}.log",
            f"{agent.replace('-', '_')}_crew.log",
        ]
        n = max(1, min(int(n or 80), 5000))  # clamp so a caller can't blow up the read
        READ_CAP = 1024 * 1024  # HARD ceiling on bytes ever loaded (1MB) — independent of n, so this can't OOM
        for name in candidates:
            p = Path("logs") / name
            try:
                if not p.is_file():
                    continue
                # Tail by BYTES — a stuck crew can grow the log to GBs; never read more than READ_CAP.
                size = p.stat().st_size
                with p.open("rb") as f:
                    if size > READ_CAP:
                        f.seek(size - READ_CAP)
                        f.readline()  # drop the partial first line after the seek
                    data = f.read(READ_CAP + 8192)  # BOUNDED read — never the whole (possibly huge) file
                lines = data.decode("utf-8", errors="replace").splitlines()[-n:]
                return {"agent": agent, "file": name, "lines": lines}
            except MemoryError:
                # the machine is momentarily out of memory — degrade gracefully, never 500 the log view
                return {"agent": agent, "file": name, "lines": ["(log temporarily unavailable — low memory)"]}
            except Exception:  # noqa: BLE001 — a locked/odd file must not crash the endpoint; try the next one
                continue
        return {"agent": agent, "file": None, "lines": []}

    @app.get("/api/fleet/{agent}/status", dependencies=[require_token])
    def agent_run_status(agent: str) -> dict:
        """One agent's live run state (running / down / stale-heartbeat / …) for the Manage-page badge.
        Local-only (process table + locks); no network."""
        from crewaimeat.tui import fleet_state

        for r in fleet_state.build_snapshot(node_index={}).rows:
            if r.agent == agent:
                return {"agent": agent, "status": r.status, "daemon": r.daemon_procs, "watchdog": r.watchdog_procs}
        return {"agent": agent, "status": "down", "daemon": 0, "watchdog": 0}

    @app.post("/api/fleet/{agent}/{action}", dependencies=[require_token])
    def fleet_action(agent: str, action: str) -> dict:
        from crewaimeat.agency import fleet_ops
        from crewaimeat.tui import actions

        fn = {"start": actions.start_crew, "stop": actions.stop_crew, "restart": actions.restart_crew}.get(action)
        if fn is None:
            raise HTTPException(status_code=400, detail="action must be start|stop|restart")
        attach = None
        if action in ("start", "restart") and brains.get_brain(agent) is not None:
            # Write the brain's crew stub (so the host finds it) AND make sure the connector has this
            # agent loaded — a brand-new agent registered after the serve daemon started isn't attached
            # until the daemon reloads. This is the missing 'approve -> run' link, now automatic.
            # ensure_bridge takes the FAST PATH when the agent is already attached (no reap, no restart,
            # no tunnel drop) and only restarts the daemon for a genuinely-new approved agent.
            brains.write_crew_stub(agent)
            attach = fleet_ops.ensure_bridge(agent)
        result = fn(agent)
        events.record(agent, action, {"result": (result or "")[:200]})
        return {"agent": agent, "action": action, "result": result, "attach": attach}

    # ── local memory (browser + Sync view) ─────────────────────────────────────
    @app.get("/api/memory/{agent}", dependencies=[require_token])
    def memory(
        agent: str,
        topic: str | None = None,
        event: str | None = None,
        source: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        limit: int = 50,
    ) -> dict:
        return {
            "records": local_memory.browse(
                agent, topic=topic, event=event, source=source, status=status, tag=tag, limit=limit
            )
        }

    @app.get("/api/memory/{agent}/facets", dependencies=[require_token])
    def memory_facets(agent: str) -> dict:
        return local_memory.facets(agent)

    @app.get("/api/memory/{agent}/record/{rid}", dependencies=[require_token])
    def memory_record(agent: str, rid: str) -> dict:
        r = local_memory.recall(agent, rid)
        if r is None:
            raise HTTPException(status_code=404, detail=f"no record '{rid}'")
        return r

    @app.post("/api/memory/{agent}/publish", dependencies=[require_token])
    def memory_publish(agent: str, body: PublishIn) -> dict:
        res = local_memory.publish(agent, body.id, key=body.key, visibility=body.visibility)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("error", "publish failed"))
        return res

    @app.get("/api/sync/{agent}", dependencies=[require_token])
    def sync_view(agent: str) -> dict:
        """The Sync view's data: local scratch vs what's ACTUALLY published on aimeat.io. Published is read
        from the NODE's own memory keys (not just the local tier) — so it includes the deliverables the
        scaffold publishes directly (crews.<agent>.…latest_output, watch.<agent>.…). Internal keys
        (.live / config / readme / offers / statistics) are filtered out so only real outputs show."""
        from crewaimeat.aimeat_crew import _aimeat_call

        raw = local_memory.browse(agent, status="raw", limit=1000)
        r = _aimeat_call(agent, "aimeat_memory_list", {})
        items = (r.get("items") if isinstance(r, dict) else None) or []
        node = []
        for it in items:
            k = it.get("key") or ""
            if not k:
                continue
            internal = (
                k.endswith(".live")
                or ".statistics" in k
                or k.startswith("agents.config")
                or k.endswith(".readme")
                or k.endswith(".offers")
                or k.endswith(".runtime")
            )
            is_output = (".latest_output" in k) or k.startswith(f"watch.{agent}") or it.get("visibility") == "public"
            if is_output and not internal:
                node.append(
                    {
                        "key": k,
                        "visibility": it.get("visibility"),
                        "updated": it.get("updated_at"),
                        "created": it.get("created_at"),
                    }
                )
        node.sort(key=lambda x: x.get("updated") or "", reverse=True)
        return {
            "agent": agent,
            "raw_count": len(raw),
            "in_sync": len(raw) == 0,
            "published_count": len(node),
            "published": node,
            "attached": r is not None,
        }

    @app.get("/api/agents/{agent}/key", dependencies=[require_token])
    def read_node_key(agent: str, key: str = Query(...)) -> dict:
        """Read one of the agent's published memory keys ON THE NODE — so the Sync view can show the actual
        deliverable (a news summary, etc.) for any key."""
        import json as _json

        from crewaimeat.aimeat_crew import _aimeat_call

        r = _aimeat_call(agent, "aimeat_memory_read", {"key": key})
        val = (r.get("value") if isinstance(r, dict) else r) if r else None
        text = (
            val if isinstance(val, str) else (None if val is None else _json.dumps(val, ensure_ascii=False, indent=1))
        )
        return {"key": key, "value": text}

    return app


def main() -> None:
    import uvicorn

    shell_launched = bool(os.environ.get(_TOKEN_ENV))
    token = os.environ.get(_TOKEN_ENV) or secrets.token_urlsafe(32)
    os.environ[_TOKEN_ENV] = token  # so create_app() inside the worker sees the same one
    host = os.environ.get("AIMEAT_AGENCY_HOST", "127.0.0.1")  # 127.0.0.1 only — never bind public
    port = int(os.environ.get("AIMEAT_AGENCY_PORT", "8753"))
    if shell_launched:  # the shell knows the token; don't echo it into the visible console
        print(f"[aimeat-agency cockpit] http://{host}:{port}  (token from env)", flush=True)
    else:  # standalone/dev: the ?boot= URL is the only way in — print it
        print(f"[aimeat-agency cockpit] http://{host}:{port}/?boot={token}", flush=True)
    uvicorn.run(create_app(token), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
