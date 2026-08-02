"""One place that builds an `ai_provenance` block, so `provider` works on both package versions.

WHY THIS EXISTS. `provider` (who SERVED the model — the router, as opposed to which model wrote it)
became fillable end-to-end when the node deployed 45d95840. The Python side of that chain is in
aimeat-crewai's source, but it ships under version **0.18.0 — the same version already on PyPI
WITHOUT the parameter**. So `declare(provider=...)` raises TypeError on a released install and works
on a source install, and no pin can tell the two apart.

The block is a plain dict DTO, so the version-proof move is to build it with `declare()` (which keeps
its validation of level/method/human_involvement — the fields where a typo matters) and attach
`provider` afterwards. Identical wire format either way, no version branching, and nothing to unwind
when the released package catches up: this module keeps working, and can then simply forward.

Sending `provider` early is safe by design — the node drops keys it does not know — so this was
correct before the deploy and is correct after it.
"""

from __future__ import annotations

from typing import Any

from aimeat_crewai.provenance import declare


def declare_block(*args: Any, provider: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """`declare(...)` plus an optional `provider`, on any aimeat-crewai 0.18.0.

    Prefers the native keyword when the installed package accepts it, so a source/newer install takes
    the supported path and this shim becomes a pass-through. Falls back to attaching the key, which
    produces the byte-identical block."""
    if provider:
        try:
            return declare(*args, provider=provider, **kwargs)
        except TypeError:
            block = declare(*args, **kwargs)
            block["provider"] = provider
            return block
    return declare(*args, **kwargs)
