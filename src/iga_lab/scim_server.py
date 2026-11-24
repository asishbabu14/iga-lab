"""Tiny local SCIM-like service. This process is the MOCK:scim target."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response


def create_app() -> FastAPI:
    """Build the mock service. State lives in memory and is discarded on exit."""
    app = FastAPI(title="MOCK:scim", docs_url=None, redoc_url=None)
    app.state.users = {}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "target": "MOCK:scim",
            "notice": "Synthetic lab target. Not a live tenant.",
        }

    @app.get("/scim/v2/Users")
    def list_users(appId: str | None = None) -> dict[str, object]:
        users = list(app.state.users.values())
        if appId:
            users = [user for user in users if user.get("appId") == appId]
        users.sort(key=lambda user: str(user.get("id")))
        return {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
            "totalResults": len(users),
            "mockTarget": "MOCK:scim",
            "Resources": users,
        }

    @app.post("/scim/v2/Users", status_code=201)
    async def create_user(request: Request) -> dict[str, object]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON object required")
        user_id = body.get("id")
        if not body.get("userName") or not user_id:
            raise HTTPException(status_code=400, detail="userName and id are required")
        if user_id in app.state.users:
            raise HTTPException(status_code=409, detail="user exists")
        stored = dict(body)
        stored["mockTarget"] = "MOCK:scim"
        app.state.users[str(user_id)] = stored
        return stored

    @app.get("/scim/v2/Users/{user_id}")
    def get_user(user_id: str) -> dict:
        user = app.state.users.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="not found")
        return user

    @app.put("/scim/v2/Users/{user_id}")
    async def replace_user(user_id: str, request: Request) -> dict[str, object]:
        if user_id not in app.state.users:
            raise HTTPException(status_code=404, detail="not found")
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON object required")
        stored = dict(body)
        stored["id"] = user_id
        stored["mockTarget"] = "MOCK:scim"
        app.state.users[user_id] = stored
        return stored

    @app.delete("/scim/v2/Users/{user_id}")
    def delete_user(user_id: str) -> Response:
        if user_id not in app.state.users:
            raise HTTPException(status_code=404, detail="not found")
        del app.state.users[user_id]
        return Response(status_code=204)

    return app


def pick_port() -> int:
    """Ask the kernel for a free local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ScimServer:
    """Run the mock service on localhost for the demo and for live tests."""

    def __init__(self, port: int | None = None) -> None:
        self.port = port if port is not None else pick_port()
        self.app = create_app()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = False
        thread = threading.Thread(target=server.run, name="mock-scim", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        deadline = time.time() + 5
        last_error = "server did not answer"
        while time.time() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{self.port}/health", timeout=0.2)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                time.sleep(0.05)
                continue
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
            time.sleep(0.05)
        raise RuntimeError(f"MOCK:scim did not become ready: {last_error}")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=10.0)
