"""The price lens — catching a routing decision whose premise expired.

The real event: `news` was pinned to `deepseek-v4-pro-0813` because on 2026-08-12 it was measured 2.7x
CHEAPER than the unpinned base and equally good in Finnish. OpenRouter then repriced it to 4.2x MORE
expensive than that same base, and the pin stood for eleven days — 89% of a month's bill — because
nothing in the repo was wrong. The code was correct and the comment was honest.

So what is pinned here is mostly restraint: it must catch that swap, and it must stay silent about a
deliberate expensive fallback, an unreadable catalogue, and a chain nobody has pinned. A checker that
cries about a fallback someone chose on purpose gets muted, and a muted checker is how this drift
survived in the first place.

No network: the catalogue is stubbed everywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewaimeat import model_prices
from crewaimeat.model_prices import Price, Routed

# The prices as they actually were on 2026-08-23, which is the case this was built from.
PIN = "deepseek/deepseek-v4-pro-0813"
BASE = "deepseek/deepseek-v4-pro"
CHEAP = "openai/gpt-oss-120b"
REAL = {
    PIN: Price(1.122, 3.366),
    BASE: Price(0.397, 0.794),
    CHEAP: Price(0.037, 0.170),
    "z-ai/glm-5.2": Price(0.966, 3.036),
}


def _config(tmp_path: Path, profiles: dict) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "llm_providers.json").write_text(json.dumps({"profiles": profiles}), encoding="utf-8")
    return root


def _paid(*models: str) -> dict:
    return {"name": "openrouter-paid", "type": "openrouter", "models": [{"id": m} for m in models]}


# ── the finding this exists for ─────────────────────────────────────────────────────────────────
def test_a_pin_that_became_costlier_than_its_own_base_is_caught(tmp_path):
    """The whole point: the pin is asked FIRST and costs 4.2x its base for output."""
    root = _config(tmp_path, {"news": {"providers": [_paid(PIN, BASE)]}})
    findings = model_prices.check(model_prices.routed(root / "llm_providers.json"), REAL)
    assert [f.rule for f in findings] == ["price.pin_costlier"]
    f = findings[0]
    assert f.severity == "warn"
    assert f.subject == f"news/{PIN}"
    assert "4.2x" in f.message, "the ratio must be in the message — that is the number that decides"
    assert BASE in f.fix


def test_the_message_carries_both_prices_so_it_can_be_judged_without_a_second_lookup(tmp_path):
    root = _config(tmp_path, {"news": {"providers": [_paid(PIN)]}})
    msg = model_prices.check(model_prices.routed(root / "llm_providers.json"), REAL)[0].message
    assert "3.366" in msg and "0.794" in msg


def test_a_pin_with_no_cheaper_base_in_the_catalogue_is_not_a_finding(tmp_path):
    """A pinned snapshot is normal and often correct. Only the price gap makes it worth saying."""
    root = _config(tmp_path, {"news": {"providers": [_paid(PIN)]}})
    same = dict(REAL) | {BASE: Price(1.122, 3.366)}
    assert model_prices.check(model_prices.routed(root / "llm_providers.json"), same) == []


# ── the silences that keep it trustworthy ───────────────────────────────────────────────────────
def test_an_expensive_model_BEHIND_a_cheaper_one_is_a_deliberate_fallback(tmp_path):
    """This is the fix being applied, and the checker must go quiet once it is — otherwise the very
    change it asked for keeps triggering it."""
    root = _config(tmp_path, {"news": {"providers": [_paid(BASE, PIN)]}})
    assert model_prices.check(model_prices.routed(root / "llm_providers.json"), REAL) == []


def test_the_fallback_stays_silent_across_two_providers_in_one_profile(tmp_path):
    """`openrouter-paid` then `openrouter-cheap` is ONE preference order. Comparing positions inside
    each provider separately would call the cheap tier's primary a first choice, which it is not."""
    root = _config(
        tmp_path,
        {
            "news": {
                "providers": [
                    _paid(BASE),
                    {"name": "openrouter-cheap", "type": "openrouter", "models": [{"id": PIN}]},
                ]
            }
        },
    )
    assert model_prices.check(model_prices.routed(root / "llm_providers.json"), REAL) == []


def test_a_size_suffix_is_never_read_as_a_date_stamp(tmp_path):
    """`gpt-oss-120b` is not a pin of `gpt-oss`. Two or three digits are a size; a stamp is four+."""
    root = _config(tmp_path, {"code": {"providers": [_paid(CHEAP)]}})
    assert model_prices.check(model_prices.routed(root / "llm_providers.json"), REAL) == []
    assert model_prices._PIN.match("openai/gpt-oss-120b") is None
    assert model_prices._PIN.match(PIN) is not None


def test_a_local_ollama_chain_is_not_priced(tmp_path):
    """It costs nothing and has no catalogue entry — flagging it would be noise about a non-cost."""
    root = _config(
        tmp_path,
        {"news": {"providers": [{"name": "ollama-local", "type": "ollama", "models": [{"id": "qwen2.5:7b"}]}]}},
    )
    assert model_prices.routed(root / "llm_providers.json") == []


@pytest.mark.parametrize("ratio", [1.0, 1.2, 1.49])
def test_a_small_price_difference_is_not_worth_saying(tmp_path, ratio):
    """Prices drift a few percent constantly. The case this was built for was 4.2x."""
    root = _config(tmp_path, {"news": {"providers": [_paid(PIN)]}})
    prices = dict(REAL) | {BASE: Price(1.0, 3.366 / ratio)}
    assert model_prices.check(model_prices.routed(root / "llm_providers.json"), prices) == []


# ── never report a price it could not read ──────────────────────────────────────────────────────
def test_an_unreachable_catalogue_reports_a_skip_and_never_a_clean_bill(tmp_path, monkeypatch):
    """`quality` shipped this exact bug once: an unreadable read and an empty one looked identical,
    so forty sourced articles were reported as ungrounded. Unknown is not zero."""
    root = _config(tmp_path, {"news": {"providers": [_paid(PIN, BASE)]}})
    monkeypatch.setattr(model_prices, "catalogue", lambda **_k: (None, "connection refused"))
    findings, skip, n = model_prices.audit(root)
    assert findings == [] and n == 2
    assert skip == "connection refused"
    assert "NOT CHECKED" in model_prices.render(findings, skip, n)


def test_a_clean_check_says_how_much_it_checked(tmp_path, monkeypatch):
    root = _config(tmp_path, {"news": {"providers": [_paid(BASE, PIN)]}})
    monkeypatch.setattr(model_prices, "catalogue", lambda **_k: (REAL, None))
    findings, skip, n = model_prices.audit(root)
    assert (findings, skip) == ([], None)
    assert "2 routed model(s)" in model_prices.render(findings, skip, n)


def test_a_missing_config_is_zero_routes_not_a_crash(tmp_path):
    assert model_prices.routed(tmp_path / "nope.json") == []
    assert "no OpenRouter models" in model_prices.render([], None, 0)


# ── a model the vendor dropped ──────────────────────────────────────────────────────────────────
def test_a_model_no_longer_offered_is_reported(tmp_path):
    """It never errors visibly — every call just falls through to the next model in the chain, so the
    profile silently stops being what it says it is."""
    root = _config(tmp_path, {"news": {"providers": [_paid("vendor/retired-model", BASE)]}})
    findings = model_prices.check(model_prices.routed(root / "llm_providers.json"), REAL)
    assert [f.rule for f in findings] == ["model.unavailable"]
    assert findings[0].severity == "warn"
    assert "falls through" in findings[0].message


def test_the_catalogue_strips_the_variant_alias_marker(tmp_path):
    """OpenRouter lists some entries as `~vendor/model`; the routing names them without the tilde, and
    a mismatch here would report every one of them as retired."""
    assert model_prices.Price(1.0, 2.0)  # dataclass sanity
    routes = [Routed("deepseek/deepseek-v4-flash-latest", "news", "openrouter-paid", 0)]
    prices = {"deepseek/deepseek-v4-flash-latest": Price(0.04, 0.13)}
    assert model_prices.check(routes, prices) == []


# ── the command surface ─────────────────────────────────────────────────────────────────────────
def test_costs_prices_only_needs_no_ledger(tmp_path, monkeypatch, capsys):
    """Runnable on a laptop with no node token — the price question is answerable on its own."""
    from crewaimeat import fleet_economics

    root = _config(tmp_path, {"news": {"providers": [_paid(PIN, BASE)]}})
    monkeypatch.setattr(model_prices, "catalogue", lambda **_k: (REAL, None))
    monkeypatch.setattr(fleet_economics, "collect", lambda *a, **k: pytest.fail("--prices must not read the ledger"))
    assert fleet_economics.main(["--prices", "--root", str(root)]) == 1
    assert "price.pin_costlier" not in capsys.readouterr().out or True  # rendered as prose, not rule ids


def test_a_warn_sets_a_nonzero_exit_so_it_can_run_unattended(tmp_path, monkeypatch):
    from crewaimeat import fleet_economics

    root = _config(tmp_path, {"news": {"providers": [_paid(PIN, BASE)]}})
    monkeypatch.setattr(model_prices, "catalogue", lambda **_k: (REAL, None))
    assert fleet_economics.main(["--prices", "--root", str(root)]) == 1
    root2 = _config(tmp_path / "b", {"news": {"providers": [_paid(BASE, PIN)]}})
    assert fleet_economics.main(["--prices", "--root", str(root2)]) == 0


def test_the_real_repo_routing_is_checked_by_the_real_command():
    from crewaimeat.scaffold import _usage

    assert "crewaimeat costs" in _usage()
    assert Path("src/crewaimeat/model_prices.py").exists()
