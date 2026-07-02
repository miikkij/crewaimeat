"""LOCOMO dataset loader — the canonical public LOng-term COnversational MEmory benchmark.

Source (stated explicitly, per the fail-loud/no-silent-truncation rule):
  - Paper: Maharana et al., "Evaluating Very Long-Term Conversational Memory of LLM Agents" (ACL 2024),
    arXiv:2402.17753.
  - Dataset: github.com/snap-research/locomo, file `data/locomo10.json` — the released 10-conversation
    subset (the canonical split every mem0-style comparison uses). ~2.8 MB, 10 conversations, 1,986 QA.

We DOWNLOAD it on first use into a gitignored cache (`benchmarks/locomo/.data/`) and print its provenance
+ sha256 so a run is auditable; we never vendor a copy into the repo. If the download fails we raise LOUD
with the manual step — never a silent empty dataset.

Category codes in locomo10.json (verified against the file; the numbering is NOT the intuitive order):
  1 = multi-hop · 2 = temporal · 3 = open-domain · 4 = single-hop · 5 = adversarial.
mem0 scores categories 1-4 and EXCLUDES 5 (adversarial rows have no ground-truth `answer`, only an
`adversarial_answer`), so `scored_qa()` filters to 1-4 to match — see metrics.SCORED_CATEGORIES.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

LOCOMO_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
LOCOMO_VERSION = "snap-research/locomo@main:data/locomo10.json"

CATEGORY_NAMES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}


def _cache_dir() -> Path:
    d = Path(__file__).resolve().parent / ".data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download(force: bool = False) -> Path:
    """Ensure locomo10.json is cached locally; return its path. Prints provenance + sha256. Fail-loud."""
    dest = _cache_dir() / "locomo10.json"
    if dest.exists() and not force:
        return dest
    print(f"[locomo] downloading dataset from {LOCOMO_URL} ...", file=sys.stderr)
    try:
        req = urllib.request.Request(LOCOMO_URL, headers={"User-Agent": "crewaimeat-locomo/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — fixed canonical https URL
            raw = r.read()
    except Exception as exc:  # noqa: BLE001 — no silent empty dataset
        raise RuntimeError(
            f"could not download LOCOMO from {LOCOMO_URL} ({type(exc).__name__}: {exc}). "
            f"Manual fix: download it yourself and save it to {dest}."
        ) from exc
    dest.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    print(
        f"[locomo] cached {len(raw)} bytes -> {dest}\n[locomo] version={LOCOMO_VERSION} sha256={sha}", file=sys.stderr
    )
    return dest


@dataclass
class QA:
    question: str
    answer: str  # gold answer as a string ("" only for adversarial rows we don't score)
    category: int
    evidence: list[str] = field(default_factory=list)  # dia_ids the answer is grounded in
    adversarial: bool = False


@dataclass
class Conversation:
    sample_id: str
    turns: list[dict]  # ordered: {speaker, text, dia_id, session, date} — flattened across sessions
    qa: list[QA]
    reference_date: str = ""  # the last session's date (mem0 injects a reference_date for temporal QA)

    def turn_texts(self) -> list[str]:
        """Each turn rendered `"Speaker (date): text"` — one memory item per turn on ingest."""
        out = []
        for t in self.turns:
            date = f" ({t['date']})" if t.get("date") else ""
            out.append(f"{t.get('speaker', '?')}{date}: {t.get('text', '')}")
        return out


def _flatten_conversation(conv: dict) -> tuple[list[dict], str]:
    """Flatten session_1, session_2, ... (in numeric order) into one ordered turn list + the last date."""
    sess_ids = sorted(
        (k for k in conv if k.startswith("session_") and not k.endswith("date_time")),
        key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0,
    )
    turns: list[dict] = []
    last_date = ""
    for sid in sess_ids:
        date = conv.get(f"{sid}_date_time", "") or ""
        if date:
            last_date = date
        for t in conv.get(sid, []) or []:
            if not isinstance(t, dict):
                continue
            turns.append(
                {
                    "speaker": t.get("speaker", "?"),
                    "text": t.get("text", "") or (t.get("blip_caption", "") if t.get("img_url") else ""),
                    "dia_id": t.get("dia_id", ""),
                    "session": sid,
                    "date": date,
                }
            )
    return turns, last_date


def _parse_qa(raw_qa: list[dict]) -> list[QA]:
    out: list[QA] = []
    for q in raw_qa or []:
        cat = q.get("category")
        try:
            cat = int(cat)
        except (TypeError, ValueError):
            continue
        adversarial = "answer" not in q
        gold = q.get("answer", q.get("adversarial_answer", ""))
        out.append(
            QA(
                question=str(q.get("question", "")).strip(),
                answer=str(gold).strip(),
                category=cat,
                evidence=[str(e) for e in (q.get("evidence") or [])],
                adversarial=adversarial,
            )
        )
    return out


def load(path: Path | None = None) -> list[Conversation]:
    """Load + parse LOCOMO into Conversation objects. Downloads on first use."""
    path = path or download()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    convs: list[Conversation] = []
    for sample in data:
        turns, ref_date = _flatten_conversation(sample.get("conversation", {}))
        convs.append(
            Conversation(
                sample_id=str(sample.get("sample_id", f"conv-{len(convs)}")),
                turns=turns,
                qa=_parse_qa(sample.get("qa", [])),
                reference_date=ref_date,
            )
        )
    return convs


def sample_conversations(convs: list[Conversation], n: int | None) -> list[Conversation]:
    """Deterministically take the first `n` conversations (n=None -> all). LOG loudly what was sampled —
    no silent truncation (the fail-loud rule)."""
    if n is None or n >= len(convs):
        print(f"[locomo] using ALL {len(convs)} conversations", file=sys.stderr)
        return convs
    n = max(1, n)
    picked = convs[:n]
    ids = ", ".join(c.sample_id for c in picked)
    print(
        f"[locomo] SAMPLED {n} of {len(convs)} conversations (deterministic first-{n}): {ids}. "
        f"Pass --full to run all {len(convs)}.",
        file=sys.stderr,
    )
    return picked


if __name__ == "__main__":  # tiny CLI: cache + summarize the dataset
    from collections import Counter

    cs = load()
    total_qa = sum(len(c.qa) for c in cs)
    cats = Counter(q.category for c in cs for q in c.qa)
    print(f"conversations: {len(cs)}  total QA: {total_qa}")
    for k in sorted(cats):
        print(f"  category {k} ({CATEGORY_NAMES.get(k, '?')}): {cats[k]}")
    print("cache:", os.fspath(_cache_dir() / "locomo10.json"))
