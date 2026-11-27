"""Request and response models for the local HTTP API."""

from pydantic import BaseModel, SecretStr

from ada_core.domain import SessionMode


class Login(BaseModel):
    token: SecretStr


class CreateSession(BaseModel):
    mode: SessionMode = SessionMode.EPHEMERAL
    title: str = ""


class ChatRequest(BaseModel):
    text: str


class ChatResult(BaseModel):
    kind: str
    text: str
    mode: str
    url: str
    notices: tuple[str, ...]
