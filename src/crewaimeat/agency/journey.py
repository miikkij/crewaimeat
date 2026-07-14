"""journey — the deterministic "where am I, what's next" ladder for the aimeat-agency copilot.

The setup wizard takes a user to "your agent is running 🎉" and then stops. This module extends that same
ladder PAST running — see the agent work → build a data app → advertise its offer → go out to aimeat.io
(share the app, explore, join an organism) — as an ordered list of steps with a single clear "next".

It is PURE (no FastAPI, no network): the cockpit gathers the real state (the setup snapshot, brains, the
built-app pointer, whether the agent has produced data) and passes it in. The result feeds BOTH the UI's
journey panel AND the copilot's grounding context — one source of truth, so the advisor can never invent a
next step the wizard would disagree with.

    from crewaimeat.agency import journey
    j = journey.compute(status, brains_list, app_state, produced_data=True, lang="fi")
    j["next"]          # the single next step {id, title, cta, ...}
"""

from __future__ import annotations

from typing import Any

# Bilingual step copy. `title` = the step label; `hint` = one short line the copilot/panel can show.
_STR: dict[str, dict[str, tuple[str, str]]] = {
    "en": {
        "account": ("Connect your account", "Tell the app which aimeat.io owner your agents belong to."),
        "engine": ("Install the engine", "The AIMEAT connector the agents run on."),
        "model": ("Set up a model", "A local model (Ollama) or an OpenRouter key."),
        "agent": ("Create your first agent", "Pick a template and describe what you want it to do."),
        "approve": ("Approve the agent", "Approve it on aimeat.io so it can act for you."),
        "running": ("Start the agent", "Bring it online so it can work."),
        "produced_data": ("See it work", "Run it once so it produces something to show."),
        "data_app": ("Build an app for its data", "A web app on aimeat.io that shows what your agent produces."),
        "publish_offer": ("Offer it to others", "Advertise your agent so others can order what it does."),
        "aimeat_share": ("Share your app", "Open your data app and share the link."),
        "aimeat_explore": ("Explore aimeat.io", "See what others built and find agents to work with."),
    },
    "fi": {
        "account": ("Yhdistä tilisi", "Kerro sovellukselle minkä aimeat.io-omistajan alle agenttisi kuuluvat."),
        "engine": ("Asenna moottori", "AIMEAT-connector jolla agentit toimivat."),
        "model": ("Ota malli käyttöön", "Paikallinen malli (Ollama) tai OpenRouter-avain."),
        "agent": ("Luo ensimmäinen agentti", "Valitse pohja ja kuvaile mitä haluat sen tekevän."),
        "approve": ("Hyväksy agentti", "Hyväksy se aimeat.io:ssa, jotta se voi toimia puolestasi."),
        "running": ("Käynnistä agentti", "Tuo se online-tilaan, jotta se voi työskennellä."),
        "produced_data": ("Katso se toiminnassa", "Aja se kerran, jotta se tuottaa jotain näytettävää."),
        "data_app": ("Rakenna sovellus datalle", "Web-sovellus aimeat.io:ssa joka näyttää mitä agenttisi tuottaa."),
        "publish_offer": ("Tarjoa muille", "Mainosta agenttiasi, jotta muut voivat tilata mitä se tekee."),
        "aimeat_share": ("Jaa sovelluksesi", "Avaa datasovelluksesi ja jaa linkki."),
        "aimeat_explore": ("Tutki aimeat.io:ta", "Katso mitä muut rakensivat ja löydä agentteja yhteistyöhön."),
    },
}


def _template_offer(brains_list: list, agent: str | None) -> bool:
    """Does the first agent's template advertise an offer (so 'offer it to others' is a real step)?"""
    if not agent:
        return False
    try:
        from crewaimeat import brain_templates

        b = next((x for x in brains_list if x.get("agent_name") == agent), None)
        tmpl = brain_templates.get(b.get("template_id")) if b else None
        return bool(tmpl and tmpl.offer)
    except Exception:  # noqa: BLE001
        return False


def compute(
    status: dict,
    brains_list: list,
    app_state: dict | None,
    *,
    produced_data: bool = False,
    lang: str = "en",
) -> dict:
    """Build the journey. Returns {steps:[{id,title,hint,done,is_next,optional,cta}], current_id, next}.

    `status` is the setup snapshot (owner_set, engine.ready, ollama.has_model, openrouter_key, brain_count,
    first_agent, first_agent_connected, first_agent_running). `app_state` is the built-app pointer for the
    first agent (or None). `produced_data` = the agent has already produced ≥1 deliverable. The terminal
    aimeat.io steps are `optional` (ongoing, never block); `next`/`current_id` skip them until every core
    step is done."""
    s = _STR.get(lang, _STR["en"])
    agent = status.get("first_agent")
    engine_ready = bool((status.get("engine") or {}).get("ready"))
    has_model = bool(
        (status.get("ollama") or {}).get("has_model") or status.get("openrouter_key") or status.get("nvidia_key")
    )
    has_app = bool(app_state and app_state.get("url"))
    app_url = (app_state or {}).get("url")
    offer_enabled = False
    b0 = next((x for x in brains_list if x.get("agent_name") == agent), None) if agent else None
    if b0:
        offer_enabled = bool((b0.get("policy") or {}).get("offer_enabled"))
    node = (status.get("node") or "https://aimeat.io").rstrip("/")

    def step(sid: str, done: bool, cta: dict, *, optional: bool = False) -> dict:
        title, hint = s.get(sid, (sid, ""))
        return {
            "id": sid,
            "title": title,
            "hint": hint,
            "done": bool(done),
            "optional": optional,
            "is_next": False,
            "cta": cta,
        }

    steps: list[dict[str, Any]] = [
        step("account", status.get("owner_set"), {"kind": "goto_step", "step": "account"}),
        step("engine", engine_ready, {"kind": "goto_step", "step": "engine"}),
        step("model", has_model, {"kind": "goto_step", "step": "model"}),
        step("agent", (status.get("brain_count") or 0) > 0, {"kind": "goto_step", "step": "agent"}),
        step("approve", status.get("first_agent_connected"), {"kind": "goto_step", "step": "approve"}),
        step("running", status.get("first_agent_running"), {"kind": "goto_step", "step": "start"}),
        step(
            "produced_data",
            produced_data,
            {"kind": "test_run", "agent": agent} if agent else {"kind": "goto_step", "step": "start"},
        ),
        step(
            "data_app",
            has_app,
            {"kind": "build_app", "agent": agent} if agent else {"kind": "goto_step", "step": "agent"},
        ),
    ]
    if _template_offer(brains_list, agent):
        steps.append(step("publish_offer", offer_enabled, {"kind": "publish_offer", "agent": agent}))
    # terminal aimeat.io band — optional / ongoing (never blocks the ladder)
    steps.append(
        step(
            "aimeat_share",
            False,
            {"kind": "open_url", "url": app_url} if app_url else {"kind": "build_app", "agent": agent},
            optional=True,
        )
    )
    steps.append(step("aimeat_explore", False, {"kind": "open_url", "url": f"{node}/discover"}, optional=True))

    # the single "next": first not-done CORE step; if all core done, the first optional step.
    nxt = next((st for st in steps if not st["done"] and not st["optional"]), None)
    if nxt is None:
        nxt = next((st for st in steps if not st["done"]), None)
    if nxt is not None:
        nxt["is_next"] = True
    return {"steps": steps, "current_id": nxt["id"] if nxt else None, "next": nxt}
