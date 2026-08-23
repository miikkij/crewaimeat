"""Does the routing still buy what it thought it was buying?

THE BUG THIS EXISTS FOR. On 2026-08-12 the `news` profile was pinned to
`deepseek/deepseek-v4-pro-0813`, and the reasoning written into the profile was sound and measured:
same Finnish quality as the unpinned base, faster, and **2.7x cheaper** ($0.435/$0.87 per M against
the base's $1.168/$2.336). It was the right call on the day it was made.

Eleven days later the prices had swapped. The pin was $1.122/$3.366 and the base $0.397/$0.794 — the
pin had become **4.2x more expensive than the model it beat**. Nothing failed, nothing logged, no test
went red. The profile's own note still argued for the pin using prices that no longer existed. It
surfaced only because a month's invoice arrived and someone read the ledger by hand: $10.10 of an
$11.39 bill, 89% of everything, for prose the cheaper sibling writes just as well.

That is a decision whose PREMISE expired, which is the hardest kind of drift to see: the code is
correct, the comment is honest, and the world moved underneath both. A lint cannot catch it because
nothing in the repo is wrong. Only asking the vendor what things cost today can.

So this asks, on every `crewaimeat costs` run:

  · is every model the routing names still on offer at all?
  · does any chain reach an expensive pinned snapshot BEFORE a materially cheaper equivalent?

WHAT IT WILL NOT DO. It never reports a price it could not read. A network failure, a rate limit or a
missing catalogue returns a skip reason, never "all clear" — the same discipline `quality` had to
learn after it reported forty well-sourced articles as ungrounded because an unreadable read and an
empty one looked identical. And it stays quiet about a deliberate expensive FALLBACK: a costly model
sitting behind a cheaper primary is a safety net someone chose, not a mistake.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

CATALOGUE_URL = "https://openrouter.ai/api/v1/models"

# A pinned snapshot is a base id plus a dated suffix: `deepseek-v4-pro-0813`, `gpt-4o-2024-08-06`.
# Four or more digits, so a genuine size suffix (`-120b`, `-30b-a3b`) is never mistaken for a date.
_PIN = re.compile(r"^(?P<base>.+?)-(?P<stamp>\d{4,})$")

# How much cheaper the sibling must be before this is worth anyone's attention. Prices move a few
# percent all the time; the case this exists for was 4.2x. A low threshold would make the check
# chatter, and a checker that chatters gets muted — which is how the original drift survived.
MATERIAL = 1.5


@dataclass(frozen=True)
class Price:
    prompt: float  # $ per 1M input tokens
    completion: float  # $ per 1M output tokens


@dataclass(frozen=True)
class Routed:
    """One model id as the routing actually reaches it."""

    model: str
    profile: str
    provider: str
    index: int  # position in its provider's chain — 0 is what gets asked first
    provider_index: int = 0  # position of the PROVIDER in the profile, as declared

    @property
    def rank(self) -> tuple[int, int]:
        """Where the routing actually reaches this model. A profile's providers are a preference
        order in the order they are WRITTEN — sorting them by name would put `openrouter-cheap`
        ahead of `openrouter-paid` and call the cheap tier's primary a first choice."""
        return (self.provider_index, self.index)


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str  # "warn" — worth changing today; "note" — worth knowing
    subject: str  # profile/model, so two findings about one route collapse to one line
    message: str
    fix: str


def catalogue(timeout: int = 60) -> tuple[dict[str, Price] | None, str | None]:
    """(prices by model id, skip_reason). Unauthenticated — the catalogue is public.

    Returns (None, reason) when it could not be read. Never (empty, None): an empty catalogue and an
    unreachable one mean opposite things, and only one of them is safe to act on.
    """
    try:
        import requests

        r = requests.get(CATALOGUE_URL, timeout=timeout)
        r.raise_for_status()
        data = r.json().get("data")
    except Exception as exc:  # noqa: BLE001 — an unreachable vendor is a result, not a crash
        return None, f"could not read {CATALOGUE_URL}: {exc!r}"
    if not data:
        return None, f"{CATALOGUE_URL} returned no models"

    prices: dict[str, Price] = {}
    for m in data:
        mid = str(m.get("id") or "").lstrip("~")  # `~vendor/model` marks a variant alias
        p = m.get("pricing") or {}
        try:
            prices[mid] = Price(float(p.get("prompt") or 0) * 1e6, float(p.get("completion") or 0) * 1e6)
        except (TypeError, ValueError):
            continue
    return prices or None, None if prices else "no usable pricing in the catalogue"


def routed(config: Path) -> list[Routed]:
    """Every model the routing can reach, in the order it reaches them.

    Only OpenRouter chains: a local ollama model has no catalogue price and flagging it would be
    noise about something that costs nothing.
    """
    try:
        doc = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[Routed] = []
    for profile, spec in (doc.get("profiles") or {}).items():
        for pi, prov in enumerate(spec.get("providers") or []):
            if str(prov.get("type") or "") != "openrouter":
                continue
            for i, m in enumerate(prov.get("models") or []):
                mid = str(m.get("id") or "").strip()
                if mid:
                    out.append(Routed(mid, str(profile), str(prov.get("name") or "?"), i, pi))
    return out


def check(routes: list[Routed], prices: dict[str, Price]) -> list[Finding]:
    """The two questions, asked per profile.

    Positions matter: a chain is a preference order, so an expensive model is only a finding when the
    routing REACHES it before something equivalent and cheaper.
    """
    findings: list[Finding] = []
    by_profile: dict[str, list[Routed]] = {}
    for r in routes:
        by_profile.setdefault(r.profile, []).append(r)

    for profile, rs in sorted(by_profile.items()):
        # `openrouter-paid` then `openrouter-cheap` is one preference order, not two.
        order = sorted(rs, key=lambda r: r.rank)
        position = {r.model: i for i, r in enumerate(order)}

        for r in order:
            if r.model not in prices:
                findings.append(
                    Finding(
                        rule="model.unavailable",
                        severity="warn",
                        subject=f"{profile}/{r.model}",
                        message=(
                            f"profile '{profile}' routes to '{r.model}', which OpenRouter no longer lists. "
                            "Every call to it falls through to the next model in the chain."
                        ),
                        fix="pick a current model id, or drop it from the chain",
                    )
                )
                continue

            m = _PIN.match(r.model)
            if not m:
                continue
            base = m.group("base")
            if base not in prices:
                continue
            pin_p, base_p = prices[r.model], prices[base]
            if base_p.completion <= 0 or pin_p.completion < base_p.completion * MATERIAL:
                continue

            # A costly model BEHIND a cheaper one is a deliberate fallback. Only flag it when the
            # routing actually reaches the pin first.
            base_pos = position.get(base)
            if base_pos is not None and base_pos < position[r.model]:
                continue

            ratio = pin_p.completion / base_p.completion
            where = "is the first model asked" if position[r.model] == 0 else "is reached before it"
            findings.append(
                Finding(
                    rule="price.pin_costlier",
                    severity="warn" if position[r.model] == 0 else "note",
                    subject=f"{profile}/{r.model}",
                    message=(
                        f"profile '{profile}' {where}: pinned '{r.model}' costs "
                        f"${pin_p.prompt:.3f}/${pin_p.completion:.3f} per M (in/out) while its base "
                        f"'{base}' costs ${base_p.prompt:.3f}/${base_p.completion:.3f} — "
                        f"{ratio:.1f}x more for output."
                    ),
                    fix=f"put '{base}' first and keep '{r.model}' behind it, or re-measure and record why the pin still wins",
                )
            )
    findings.sort(key=lambda f: (f.severity != "warn", f.subject))
    return findings


def render(findings: list[Finding], skip: str | None, routes: int) -> str:
    if skip:
        return f"  model prices: NOT CHECKED — {skip}"
    if not routes:
        return "  model prices: no OpenRouter models in llm_providers.json"
    if not findings:
        return f"  model prices: {routes} routed model(s) checked against OpenRouter — nothing overpriced"
    out = [f"  model prices — {len(findings)} finding(s) across {routes} routed model(s):", ""]
    for f in findings:
        out.append(f"    [{f.severity}] {f.subject}")
        out.append(f"      {f.message}")
        out.append(f"      fix: {f.fix}")
    out.append("")
    out.append("  A routing decision can be right when made and wrong a month later; prices move.")
    return "\n".join(out)


def audit(root: Path) -> tuple[list[Finding], str | None, int]:
    """(findings, skip_reason, routed_model_count) — the whole check, for `costs` and for tests."""
    routes = routed(root / "llm_providers.json")
    if not routes:
        return [], None, 0
    prices, skip = catalogue()
    if prices is None:
        return [], skip, len(routes)
    return check(routes, prices), None, len(routes)
