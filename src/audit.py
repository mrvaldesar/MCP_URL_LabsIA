import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("grader")
_CREDENTIAL = re.compile(r"(?i)\b(?:[0-9a-f]{32}\.[0-9a-f]{64}|[0-9a-f]{32})\b")
_SECRET_FIELD = re.compile(
    r"""(?i)(["']?(?:authorization|team_token|x-team-token)["']?\s*[:=]\s*)"""
    r"""(?:"[^"]*"|'[^']*'|(?:Bearer\s+)?[^\s,;}]+)"""
)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        for key in ("ADMIN_TOKEN", "GITHUB_API_TOKEN"):
            credential = os.getenv(key)
            if credential:
                message = message.replace(credential, "[REDACTED]")
        message = _SECRET_FIELD.sub(r"\1[REDACTED]", message)
        message = _CREDENTIAL.sub("[REDACTED]", message)
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": message,
        }, ensure_ascii=False)


@contextmanager
def audit_log():
    path = Path(os.getenv("GRADER_LOG_PATH", "grader.log"))
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        handler.close()


class AuditMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def audit_send(message: Message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                # Keep headers, query strings and bodies out of the request log.
                logger.info("HTTP %s %s status=%s", scope["method"], scope["path"], message["status"])
            await send(message)

        try:
            await self.app(scope, receive, audit_send)
        except Exception:
            logger.exception("Error interno al procesar la solicitud HTTP")
            if started:
                raise
            response = JSONResponse({"error": "Error interno del servidor."}, status_code=500)
            await response(scope, receive, audit_send)
