"""Images, vision and embeddings through the node, when the agent's model road is the node.

WHY THIS EXISTS. The crews' text calls already follow the owner's choice (crewaimeat.llm, the node
road): the node picks the model and the key, the agent's own then the owner's then the place's, and
records the call. Six other call sites did not: Seedream image generation, the moodboard and concierge
vision reads, the browser tool's screenshot describe, and the memory embeddings all called OpenRouter
with OPENROUTER_API_KEY from this machine's environment. On a hosted place that is the PLACE's key,
paying outside the node's metering, so an owner's own key never paid for them -- and the key could
not leave the container (brief doc-muqud1ahihw8, step 3).

THE RULE, the same one the text road keeps:
  * the agent's road is the node -> the node's own door, with the agent's credential (through the serve
    daemon, which attaches it): POST /v1/ai/image, POST /v1/ai/complete with `images` (the node picks
    its vision model when a call carries images), POST /v1/ai/embed. The node picks the model; a
    refusal comes back in the node's own code and words. Nothing falls back to this machine's key.
  * not the node road and OPENROUTER_API_KEY is set -> the caller's direct path, as before.
  * neither -> the call fails, and says which two things would make it work (`no_route`).

`road(agent)` decides; each caller asks it once per call. Agent identity for a caller that does not
thread one comes from the ledger's per-kickoff context (ledger_report._resolve_agent).
"""

from __future__ import annotations

import base64
import os
import sys
from typing import Any

NODE = "node"
MACHINE = "machine"
NONE = "none"


class NodeAiError(RuntimeError):
    """The node refused or did not answer. `code` is the node's own (or NO_ANSWER)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code or "REFUSED"
        self.node_message = message
        super().__init__(f"{self.code}: {message}")


def agent_of(agent: str | None) -> str | None:
    """The agent a call runs as: the one given, else the crew kickoff's own (ledger context)."""
    if agent:
        return agent
    try:
        from crewaimeat.ledger_report import _resolve_agent

        return _resolve_agent(None)
    except Exception:  # noqa: BLE001 -- no context is an answer: no agent
        return None


def _on_node_road(who: str) -> bool:
    """The owner's road for `who`, read the way the text road reads it (crewaimeat.llm). The seam the
    offline tests stub: answering it means asking the node."""
    from crewaimeat.llm import _node_road_choice

    return _node_road_choice(who) is not None


def road(agent: str | None) -> str:
    """NODE when the owner routes this agent's model calls through the node; else MACHINE when this
    machine holds OPENROUTER_API_KEY; else NONE."""
    who = agent_of(agent)
    if who:
        try:
            if _on_node_road(who):
                return NODE
        except Exception as exc:  # noqa: BLE001 -- the text road decides the same way; say it, do not guess
            print(f"[node-ai] {who}: could not read the owner's model road ({exc!r})", file=sys.stderr)
    return MACHINE if os.getenv("OPENROUTER_API_KEY") else NONE


def no_route(what: str, agent: str | None) -> str:
    """The sentence for a call with neither road."""
    who = agent_of(agent) or "this agent"
    return (
        f"{what} unavailable for {who}: its model road is not the node, and OPENROUTER_API_KEY is not set "
        "on this machine. The owner can route the agent through the node (its own key then pays), or the "
        "machine sets a key."
    )


def _post(agent: str, path: str, body: dict, *, retries: int = 2) -> dict:
    from crewaimeat.aimeat_crew import _aimeat_rest

    res = _aimeat_rest(agent, "POST", path, body, retries=retries, return_error=True)
    if res is None:
        raise NodeAiError("NO_ANSWER", f"the node did not answer {path}")
    if isinstance(res, dict) and res.get("ok") is False:
        err = res.get("error") if isinstance(res.get("error"), dict) else {}
        raise NodeAiError(str(err.get("code") or res.get("http_status") or "REFUSED"), str(err.get("message") or res))
    return res if isinstance(res, dict) else {}


def data_url(image: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(image).decode("ascii")


def generate_image(agent: str, prompt: str, *, public: bool = True, storage_key: str | None = None) -> dict:
    """POST /v1/ai/image. The node chooses the model and stores the picture; the answer is
    {storage_key, mime_type, size, model, visibility, url}. No retry: a provider may bill a generation
    the node stopped reading, so a second POST could pay twice."""
    body: dict[str, Any] = {"prompt": prompt, "public": bool(public)}
    if storage_key:
        body["storage_key"] = storage_key
    return _post(agent, "/v1/ai/image", body, retries=1)


def complete_with_images(agent: str, prompt: str, images: list[str], *, system: str | None = None) -> str:
    """POST /v1/ai/complete with `images` (data: or https URLs). The node picks its vision model."""
    body: dict[str, Any] = {"prompt": prompt, "images": list(images)[:8]}
    if system:
        body["systemPrompt"] = system
    data = _post(agent, "/v1/ai/complete", body)
    model = data.get("model")
    if model:
        print(f"[node-ai] {agent}: vision answered by {model}", file=sys.stderr)
    return str(data.get("content") or "")


def embed_answer(agent: str, texts: list[str]) -> dict:
    """POST /v1/ai/embed: {embeddings, model, dimensions, ...}, one vector per text, in order."""
    data = _post(agent, "/v1/ai/embed", {"input": list(texts)})
    vectors = data.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise NodeAiError("BAD_ANSWER", f"/v1/ai/embed returned {type(vectors).__name__} for {len(texts)} texts")
    return data


def embed(agent: str, texts: list[str]) -> list[list[float]]:
    return embed_answer(agent, texts)["embeddings"]


def node_embedder(agent: str) -> tuple[dict, str]:
    """A CrewAI embedder spec (`provider: custom`) whose vectors come from the node's /v1/ai/embed as
    `agent`, and the storage tag for it. CrewAI instantiates the callable with the spec's extra config
    keys, so the agent name travels in the config.

    THE TAG NAMES THE MODEL AND ITS WIDTH. The node picks the embedding model, and the owner can change
    it; a vector store written at one width and read at another is corrupt, not merely stale. So one
    short probe asks the node which model answers now, and the tag (folded into the storage path)
    carries it: a changed model opens a new store instead of breaking the old one. Raises NodeAiError
    when the node refuses or does not answer -- a crew that asked for memory must not run without it.
    """
    from crewai.rag.embeddings.providers.custom.embedding_callable import CustomEmbeddingFunction

    probe = embed_answer(agent, ["probe"])
    model = str(probe.get("model") or "unknown")
    dims = probe.get("dimensions") or len(probe["embeddings"][0] or [])
    tag = "node-" + "".join(c if c.isalnum() or c in "-." else "-" for c in model.lower()) + f"-{dims}"

    class NodeEmbedding(CustomEmbeddingFunction):
        def __init__(self, agent_name: str = agent, **_kw: Any) -> None:
            self._agent = agent_name

        def __call__(self, input):  # noqa: A002, ANN001 -- the chromadb protocol's own argument name
            import numpy as np

            texts = [input] if isinstance(input, str) else list(input)
            return [np.asarray(v, dtype=np.float32) for v in embed(self._agent, texts)]

        @staticmethod
        def name() -> str:
            return "aimeat-node"

    return {"provider": "custom", "config": {"embedding_callable": NodeEmbedding, "agent_name": agent}}, tag


def pub_owner_of(url: str) -> str | None:
    """The identity in a `/v1/pub/<id>/<key>` URL, for a caller that attaches by (id, key)."""
    import urllib.parse

    marker = "/v1/pub/"
    if marker not in (url or ""):
        return None
    rest = url.split(marker, 1)[1]
    return urllib.parse.unquote(rest.split("/", 1)[0]) or None
