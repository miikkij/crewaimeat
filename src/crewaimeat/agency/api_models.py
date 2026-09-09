"""Request schemas shared by the cockpit route groups."""

from __future__ import annotations

from pydantic import BaseModel

DEFAULT_OLLAMA_MODEL = "gemma4"


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


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None
    lang: str = "en"


class AppGenPromptIn(BaseModel):
    idea: str = ""
    template: str | None = None
    lang: str = "en"


class BrainGenIn(BaseModel):
    description: str = ""
    lang: str = "en"


class BrainGenCreateIn(BaseModel):
    template: dict  # the (edited) generated template JSON: {template: <header>, crew: <crew def>}
    agent_name: str
    prose: str | None = None
    policy: dict | None = None
    title: str | None = None


class AppGenPublishIn(BaseModel):
    html: str
    name: str = ""
    agent: str | None = None
