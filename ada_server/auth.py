"""Validate the local API credential and issue expiring signed cookies."""

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request, Response

from ada_core.errors import redact_secrets


COOKIE = "ada_session"
COOKIE_TTL = 7 * 24 * 60 * 60


class TokenAuth:
    def __init__(self, token: str, *, cookie_secure: bool, cookie_ttl: int):
        if len(token) < 16 or not token.strip():
            raise ValueError("ADA_TOKEN must contain at least 16 characters and not be blank")
        if cookie_ttl <= 0:
            raise ValueError("Cookie lifetime must be positive")
        self._token = token
        self._key = token.encode("utf-8")
        self._ttl = cookie_ttl
        self._flags = dict(httponly=True, secure=cookie_secure, samesite="strict", path="/")

    @staticmethod
    def unauthorized() -> HTTPException:
        return HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})

    def matches(self, token: str) -> bool:
        return hmac.compare_digest(self._key, token.encode("utf-8", "surrogatepass"))

    def _signature(self, payload: str) -> str:
        return hmac.new(self._key, f"ada_session:{payload}".encode("ascii"), hashlib.sha256).hexdigest()

    def authorized(self, request: Request) -> bool:
        authorization = request.headers.get("authorization")
        if authorization is not None:
            scheme, _, credential = authorization.partition(" ")
            return scheme.lower() == "bearer" and self.matches(credential)
        cookie = request.cookies.get(COOKIE, "")
        payload, _, signature = cookie.rpartition(".")
        expires, _, nonce = payload.partition(".")
        return (cookie.isascii() and len(expires) <= 20 and expires.isdecimal() and bool(nonce)
                and hmac.compare_digest(self._signature(payload), signature)
                and int(expires) > time.time())

    def login(self, response: Response, token: str) -> None:
        if not self.matches(token):
            raise self.unauthorized()
        payload = f"{int(time.time()) + self._ttl}.{secrets.token_urlsafe(16)}"
        response.set_cookie(COOKIE, f"{payload}.{self._signature(payload)}",
                            max_age=self._ttl, **self._flags)

    def logout(self, response: Response) -> None:
        response.delete_cookie(COOKIE, **self._flags)

    def redact(self, text: str) -> str:
        return redact_secrets(text, (self._token,), include_escaped=True)
