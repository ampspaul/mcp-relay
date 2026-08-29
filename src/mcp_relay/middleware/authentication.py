"""Inbound bearer-token authentication middleware."""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..observability import metrics

logger = logging.getLogger(__name__)

# Only /health bypasses auth so load-balancer probes always succeed.
# /metrics is intentionally NOT exempt — it exposes tool names, call counts,
# and error rates that can profile the deployment.
_EXEMPT = {"/health"}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests that do not carry a valid Bearer token.

    Skips /health and /metrics so load-balancer probes always succeed.
    Tokens are resolved at startup and passed in as a plain set of strings.
    """

    def __init__(self, app, valid_tokens: set[str]) -> None:
        super().__init__(app)
        self._tokens = valid_tokens

    async def dispatch(self, request: Request, call_next):
        if not self._tokens or request.url.path in _EXEMPT:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning(
                "[auth] inbound: missing or malformed Authorization header path=%s client=%s",
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            metrics.increment("inbound_auth_rejected_total", reason="missing_token")
            return JSONResponse(
                {
                    "error": "Unauthorized",
                    "detail": "Authorization: Bearer <token> header required",
                },
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth_header[len("Bearer ") :]
        if token not in self._tokens:
            logger.warning(
                "[auth] inbound: invalid token path=%s client=%s",
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            metrics.increment("inbound_auth_rejected_total", reason="invalid_token")
            return JSONResponse(
                {"error": "Unauthorized", "detail": "Invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        metrics.increment("inbound_auth_accepted_total")
        return await call_next(request)
