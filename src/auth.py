import json
import re

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.database import registration_retry_after, resolve_team_id

TOKEN_PATTERN = re.compile(r"(?:[0-9a-f]{32}\.[0-9a-f]{64}|[0-9a-f]{32})")
PUBLIC_METHODS = ("initialize", "notifications/initialized", "tools/list", "ping")
MAX_REQUEST_BYTES = 1024 * 1024


class TeamAuthMiddleware:
    def __init__(self, app: ASGIApp, path: str):
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or scope["path"] != self.path or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                response = JSONResponse({"error": "La solicitud supera 1 MiB."}, status_code=413)
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body"):
                break

        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            payload = None
        request = payload if isinstance(payload, dict) else {}
        method = request.get("method")
        params = request.get("params")
        registration = (
            method == "tools/call"
            and isinstance(params, dict)
            and params.get("name") == "register_team"
        )

        if registration:
            client = scope.get("client")
            ip = client[0] if client else "unknown"
            retry_after = await run_in_threadpool(registration_retry_after, ip)
            if retry_after is not None:
                response = JSONResponse(
                    {"error": "Límite de registros alcanzado. Vuelve a intentarlo más tarde."},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
                await response(scope, receive, send)
                return

        state = scope.setdefault("state", {})
        if method not in PUBLIC_METHODS and not registration:
            authorization = Headers(scope=scope).get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            team_id = None
            if scheme.lower() == "bearer" and TOKEN_PATTERN.fullmatch(token):
                team_id = await run_in_threadpool(resolve_team_id, token)
            if team_id is None:
                response = JSONResponse(
                    {"error": "Se requiere un token de equipo válido en Authorization: Bearer."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
            state["team_id"] = team_id

        # FastMCP receives the identity and the replayed request without the credential.
        scope = dict(scope, headers=[item for item in scope["headers"] if item[0] != b"authorization"])
        buffered = True

        async def receive_body() -> Message:
            nonlocal buffered
            if buffered:
                buffered = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def send_response(message: Message):
            if registration and message["type"] == "http.response.start":
                issued_token = state.pop("issued_team_token", None)
                if issued_token is not None:
                    headers = MutableHeaders(scope=message)
                    headers["X-Team-Token"] = issued_token
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive_body, send_response)
