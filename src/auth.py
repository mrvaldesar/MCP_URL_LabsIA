import json
import os
from ipaddress import ip_address, ip_network
import re
from hashlib import sha256

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.database import resolve_team_id
from src.rate_limit import SlidingWindow

TOKEN_PATTERN = re.compile(r"(?:[0-9a-f]{32}\.[0-9a-f]{64}|[0-9a-f]{32})")
PUBLIC_METHODS = ("initialize", "notifications/initialized", "tools/list", "ping")
MAX_REQUEST_BYTES = 1024 * 1024


def rate_limit_response(request: dict, retry_after: int, *, registration: bool = False) -> Response:
    message = f"Has excedido el límite de peticiones. Por favor, espera {retry_after} segundos antes de reintentar"
    headers = {"Retry-After": str(retry_after)}
    envelope = {"jsonrpc": "2.0", "id": request.get("id")}
    if registration:
        return JSONResponse(
            {**envelope, "error": {"code": -32000, "message": message}},
            status_code=429,
            headers=headers,
        )
    if "id" not in request:
        return Response(status_code=202, headers=headers)
    return JSONResponse(
        {**envelope, "result": {"isError": True, "content": [{"type": "text", "text": message}]}},
        headers=headers,
    )


def client_ip(scope: Scope, trusted_proxies) -> str:
    peer = scope.get("client")
    if not peer:
        return "unknown"
    try:
        address = ip_address(peer[0])
    except ValueError:
        return peer[0]
    for hop in reversed(Headers(scope=scope).get("x-forwarded-for", "").split(",")):
        if not any(address in network for network in trusted_proxies):
            break
        try:
            address = ip_address(hop.strip())
        except ValueError:
            break
    return str(address)


def configured_limit(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} debe ser un entero positivo.")
    return value


class TeamAuthMiddleware:
    def __init__(self, app: ASGIApp, path: str):
        self.app = app
        self.path = path
        seconds = configured_limit("REGISTER_WINDOW_SECONDS", 600)
        self.registration_limit = SlidingWindow(configured_limit("REGISTER_IP_LIMIT", 80), seconds)
        self.global_limit = SlidingWindow(configured_limit("REGISTER_GLOBAL_LIMIT", 256), seconds)
        self.carnet_limit = SlidingWindow(configured_limit("REGISTER_CARNET_FAILURE_LIMIT", 5), seconds)
        self.trusted_proxies = tuple(
            ip_network(value.strip())
            for value in os.getenv("TRUSTED_PROXY_CIDRS", "").split(",") if value.strip()
        )
        self.submission_limit = SlidingWindow(limit=10, seconds=60)

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
        tool = params.get("name") if method == "tools/call" and isinstance(params, dict) else None
        registration = tool == "register_team"

        state = scope.setdefault("state", {})
        if registration:
            ip = client_ip(scope, self.trusted_proxies)
            retry_after = self.registration_limit.retry_after(ip)
            if retry_after is None:
                retry_after = self.global_limit.retry_after("all")
            if retry_after is not None:
                await rate_limit_response(request, retry_after, registration=True)(scope, receive, send)
                return

            state["carnet_limit"] = self.carnet_limit

        if method not in PUBLIC_METHODS and not registration:
            authorization = Headers(scope=scope).get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            team_id = None
            submission_key = None
            if scheme.lower() == "bearer" and TOKEN_PATTERN.fullmatch(token):
                if tool == "submit_lab_results":
                    submission_key = sha256(token.encode("ascii")).hexdigest()
                    retry_after = self.submission_limit.retry_after(submission_key, consume=False)
                    if retry_after is not None:
                        await rate_limit_response(request, retry_after)(scope, receive, send)
                        return
                team_id = await run_in_threadpool(resolve_team_id, token)
            if team_id is None:
                response = JSONResponse(
                    {"error": "Se requiere un token de equipo válido en Authorization: Bearer."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
            if submission_key is not None:
                # Recheck atomically after authentication before forwarding the call.
                retry_after = self.submission_limit.retry_after(submission_key)
                if retry_after is not None:
                    await rate_limit_response(request, retry_after)(scope, receive, send)
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
