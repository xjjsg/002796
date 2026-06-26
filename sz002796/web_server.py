"""aiohttp application serving the V6 React dashboard and realtime API."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from .web_runtime import DashboardRuntime, load_backtest_summary, load_data_status, load_trade_history


WEB_ASSETS = Path(__file__).resolve().parent / "web_assets"
RUNTIME_KEY: web.AppKey[DashboardRuntime] = web.AppKey("dashboard_runtime", DashboardRuntime)


async def get_bootstrap(request: web.Request) -> web.Response:
    return web.json_response(request.app[RUNTIME_KEY].bootstrap())


async def get_health(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME_KEY]
    return web.json_response({"ok": True, **runtime.runtime_status()})


async def start_runtime(request: web.Request) -> web.Response:
    body: dict[str, Any] = await request.json() if request.can_read_body else {}
    status = await request.app[RUNTIME_KEY].start_worker(body.get("source"))
    return web.json_response(status)


async def stop_runtime(request: web.Request) -> web.Response:
    status = await request.app[RUNTIME_KEY].stop_worker()
    return web.json_response(status)


async def get_trades(request: web.Request) -> web.Response:
    try:
        limit = min(1000, max(1, int(request.query.get("limit", "200"))))
    except ValueError:
        limit = 200
    return web.json_response({"trades": load_trade_history(limit=limit)})


async def get_backtest(request: web.Request) -> web.Response:
    return web.json_response(load_backtest_summary())


async def get_data_status(request: web.Request) -> web.Response:
    return web.json_response(load_data_status())


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    runtime = request.app[RUNTIME_KEY]
    socket = web.WebSocketResponse(heartbeat=20)
    await socket.prepare(request)
    await runtime.register(socket)
    try:
        async for message in socket:
            if message.type == WSMsgType.TEXT:
                if message.data == "ping":
                    await socket.send_str("pong")
            elif message.type == WSMsgType.ERROR:
                break
    finally:
        runtime.unregister(socket)
    return socket


async def spa_handler(request: web.Request) -> web.StreamResponse:
    if not WEB_ASSETS.exists():
        return web.Response(
            status=503,
            text="Web UI 尚未构建。请运行 frontend 目录中的 pnpm build。",
            content_type="text/plain",
        )
    path = request.match_info.get("path", "")
    candidate = (WEB_ASSETS / path).resolve()
    if path and candidate.is_relative_to(WEB_ASSETS.resolve()) and candidate.is_file():
        return web.FileResponse(candidate)
    return web.FileResponse(WEB_ASSETS / "index.html")


async def runtime_startup(app: web.Application) -> None:
    await app[RUNTIME_KEY].start()


async def runtime_cleanup(app: web.Application) -> None:
    await app[RUNTIME_KEY].close()


def create_app(runtime: DashboardRuntime | None = None) -> web.Application:
    app = web.Application()
    app[RUNTIME_KEY] = runtime or DashboardRuntime()
    app.router.add_get("/api/bootstrap", get_bootstrap)
    app.router.add_get("/api/health", get_health)
    app.router.add_post("/api/runtime/start", start_runtime)
    app.router.add_post("/api/runtime/stop", stop_runtime)
    app.router.add_get("/api/trades", get_trades)
    app.router.add_get("/api/backtest", get_backtest)
    app.router.add_get("/api/data-status", get_data_status)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/{path:.*}", spa_handler)
    app.on_startup.append(runtime_startup)
    app.on_cleanup.append(runtime_cleanup)
    return app
