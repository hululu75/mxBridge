from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from aiohttp import web

from bridge.message_store import MessageStore
from bridge.state import StateManager

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
SESSION_LIFETIME = 86400 * 7
LOGIN_RATE_LIMIT = 10
LOGIN_RATE_WINDOW = 60


class WebServer:
    def __init__(self, store: MessageStore, config: dict, media_dir: str = "",
                 full_config: Optional[dict] = None):
        self._store = store
        self._media_dir = media_dir
        self._full_config = full_config or {}
        password = config.get("password", "")
        self._password = password if password else ""
        if not self._password:
            self._host = "127.0.0.1"
            logger.warning("Web interface: no password set, binding to 127.0.0.1 only. Set web.password to enable remote access.")
        else:
            self._host = config.get("host", "0.0.0.0")
        self._port = config.get("port", 8080)
        self._trusted_proxy = config.get("trusted_proxy", False)
        self._secret = store.get_or_create_secret()
        self._login_attempts: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.time()
        html_path = TEMPLATES_DIR / "index.html"
        self._index_html = html_path.read_text() if html_path.exists() else "<h1>Matrix Bridge Message Search</h1>"
        self._app = web.Application(middlewares=[self._auth_middleware])
        self._backfill_task: Optional[asyncio.Task] = None
        self._backfill_state: dict = {
            "running": False,
            "current_room": "",
            "processed_rooms": 0,
            "total_rooms": 0,
            "saved": 0,
            "skipped": 0,
            "error": None,
            "done": False,
        }
        self._setup_routes()

    def _setup_routes(self) -> None:
        self._app.router.add_get("/", self._index)
        self._app.router.add_post("/api/login", self._api_login)
        self._app.router.add_get("/api/stats", self._api_stats)
        self._app.router.add_get("/api/rooms", self._api_rooms)
        self._app.router.add_get("/api/rooms/{room_id}/senders", self._api_room_senders)
        self._app.router.add_get("/api/search", self._api_search)
        self._app.router.add_get("/api/history/{room_id}", self._api_history)
        self._app.router.add_get("/api/context/{event_id}", self._api_context)
        self._app.router.add_get("/api/media/{event_id}", self._api_media)
        self._app.router.add_post("/api/backfill", self._api_backfill_start)
        self._app.router.add_get("/api/backfill/status", self._api_backfill_status)
        self._app.router.add_static(
            "/static", TEMPLATES_DIR, show_index=False, follow_symlinks=False
        )

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        if not self._password:
            return await handler(request)
        path = request.path
        if path in ("/", "/api/login") or path.startswith("/static"):
            return await handler(request)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            if self._verify_token(token):
                return await handler(request)
        qtoken = request.query.get("token", "")
        if qtoken and self._verify_token(qtoken):
            return await handler(request)
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Unauthorized"}), content_type="application/json")

    def _generate_token(self) -> str:
        expiry = int(time.time()) + SESSION_LIFETIME
        payload = str(expiry)
        sig = hmac.new(self._secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{expiry}:{sig}"

    def _verify_token(self, token: str) -> bool:
        try:
            parts = token.split(":", 1)
            if len(parts) != 2:
                return False
            expiry = int(parts[0])
            if time.time() > expiry:
                return False
            payload = str(expiry)
            expected = hmac.new(self._secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, parts[1])
        except (ValueError, IndexError):
            return False

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=self._index_html, content_type="text/html")

    def _get_client_ip(self, request: web.Request) -> str:
        if self._trusted_proxy:
            forwarded_for = request.headers.get("X-Forwarded-For", "")
            if forwarded_for:
                return forwarded_for.split(",")[0].strip()
        return request.remote or "unknown"

    def _check_rate_limit(self, ip: str) -> bool:
        now = time.time()
        attempts = self._login_attempts[ip]
        self._login_attempts[ip] = [t for t in attempts if now - t < LOGIN_RATE_WINDOW]
        if now - self._last_cleanup > LOGIN_RATE_WINDOW:
            stale = [k for k, v in self._login_attempts.items() if not v]
            for k in stale:
                del self._login_attempts[k]
            self._last_cleanup = now
        return len(self._login_attempts[ip]) < LOGIN_RATE_LIMIT

    async def _api_login(self, request: web.Request) -> web.Response:
        ip = self._get_client_ip(request)
        if not self._check_rate_limit(ip):
            raise web.HTTPTooManyRequests(text=json.dumps({"error": "Too many login attempts"}), content_type="application/json")
        if not self._password:
            return web.json_response({"token": self._generate_token()})
        try:
            data = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text=json.dumps({"error": "Invalid JSON"}), content_type="application/json")
        provided = data.get("password", "")
        if not isinstance(provided, str) or not hmac.compare_digest(provided, self._password):
            self._login_attempts[ip].append(time.time())
            raise web.HTTPForbidden(text=json.dumps({"error": "Wrong password"}), content_type="application/json")
        return web.json_response({"token": self._generate_token()})

    async def _api_stats(self, request: web.Request) -> web.Response:
        result = await asyncio.to_thread(self._store.get_stats)
        return web.json_response(result)

    async def _api_rooms(self, request: web.Request) -> web.Response:
        rooms = await asyncio.to_thread(self._store.get_rooms)
        return web.json_response(rooms)

    async def _api_room_senders(self, request: web.Request) -> web.Response:
        room_id = request.match_info["room_id"]
        senders = await asyncio.to_thread(self._store.get_senders, room_id)
        return web.json_response(senders)

    async def _api_search(self, request: web.Request) -> web.Response:
        q = request.query.get("q", "")
        room = request.query.get("room", "")
        sender = request.query.get("sender", "")
        date_from = request.query.get("from")
        date_to = request.query.get("to")
        try:
            page = int(request.query.get("page", "1"))
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            page = 1
            limit = 50
        result = await asyncio.to_thread(
            self._store.search_messages,
            q, room, sender, date_from, date_to,
            max(1, page), min(200, max(1, limit)),
        )
        return web.json_response(result)

    async def _api_history(self, request: web.Request) -> web.Response:
        room_id = request.match_info["room_id"]
        try:
            page = int(request.query.get("page", "1"))
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            page = 1
            limit = 50
        result = await asyncio.to_thread(
            self._store.get_room_history, room_id, max(1, page), min(200, max(1, limit))
        )
        return web.json_response(result)

    async def _api_context(self, request: web.Request) -> web.Response:
        event_id = request.match_info["event_id"]
        try:
            before = min(50, max(1, int(request.query.get("before", "25"))))
            after = min(50, max(1, int(request.query.get("after", "25"))))
        except ValueError:
            before = after = 25
        result = await asyncio.to_thread(self._store.get_message_context, event_id, before, after)
        if not result:
            raise web.HTTPNotFound(text=json.dumps({"error": "Message not found"}), content_type="application/json")
        return web.json_response(result)

    async def _api_media(self, request: web.Request) -> web.Response:
        event_id = request.match_info["event_id"]
        logger.debug("Media request: event_id=%s, media_dir=%s", event_id, self._media_dir)
        if not self._media_dir:
            raise web.HTTPNotFound(text=json.dumps({"error": "Media storage not configured"}), content_type="application/json")
        local_path = await asyncio.to_thread(self._store.get_media_path, event_id)
        logger.debug("Media local_path=%s", local_path)
        if not local_path:
            raise web.HTTPNotFound(text=json.dumps({"error": "Media not found"}), content_type="application/json")
        full_path = os.path.realpath(os.path.join(self._media_dir, local_path))
        logger.debug("Media full_path=%s", full_path)
        if not full_path.startswith(os.path.realpath(self._media_dir) + os.sep):
            raise web.HTTPNotFound(text=json.dumps({"error": "Invalid path"}), content_type="application/json")
        if not os.path.isfile(full_path):
            raise web.HTTPNotFound(text=json.dumps({"error": "Media file missing"}), content_type="application/json")
        return web.FileResponse(full_path)

    async def _api_backfill_start(self, request: web.Request) -> web.Response:
        if self._backfill_state["running"]:
            return web.json_response({"error": "Backfill already running"}, status=409)
        try:
            data = await request.json()
        except Exception:
            data = {}

        source_config = self._full_config.get("source", {})
        if not source_config.get("homeserver"):
            return web.json_response({"error": "Source not configured"}, status=400)

        bridge_config = self._full_config.get("bridge", {})
        store_cfg = bridge_config.get("message_store", {})

        days = data.get("days", 0)
        if isinstance(days, str):
            try:
                days = int(days)
            except ValueError:
                days = 0
        no_media = bool(data.get("no_media", False))
        clear_before = bool(data.get("clear_before", False))

        self._backfill_state = {
            "running": True,
            "current_room": "",
            "processed_rooms": 0,
            "total_rooms": 0,
            "saved": 0,
            "skipped": 0,
            "error": None,
            "done": False,
            "params": {"days": days, "no_media": no_media, "clear_before": clear_before},
        }

        self._backfill_task = asyncio.create_task(
            self._run_backfill(source_config, bridge_config, store_cfg, days, no_media, clear_before)
        )
        return web.json_response({"status": "started"})

    async def _api_backfill_status(self, request: web.Request) -> web.Response:
        return web.json_response(self._backfill_state)

    async def _run_backfill(self, source_config: dict, bridge_config: dict,
                            store_cfg: dict, days: int, no_media: bool,
                            clear_before: bool) -> None:
        from backfill import _init_client, backfill_room, _get_room_name
        client = None
        try:
            if clear_before:
                logger.info("Backfill: clearing existing data")
                await asyncio.to_thread(self._store.clear_all)
                if self._media_dir and os.path.isdir(self._media_dir):
                    await asyncio.to_thread(self._clear_media_dir, self._media_dir)

            state_path = bridge_config.get("state_path", "state.json")
            state = StateManager(state_path)
            await state.load()

            logger.info("Backfill: connecting to source server")
            client = await _init_client(source_config, state)

            joined_rooms = dict(client.rooms)
            room_list = list(joined_rooms.items())
            self._backfill_state["total_rooms"] = len(room_list)

            args = argparse.Namespace(
                days=days,
                limit=0,
                no_media=no_media,
                dry_run=False,
                media_dir=store_cfg.get("media_dir", "") if not no_media else "",
                media_max_size=source_config.get("media_max_size", 50 * 1024 * 1024),
            )

            if args.media_dir and not no_media:
                os.makedirs(args.media_dir, exist_ok=True)

            total_saved = 0
            for i, (room_id, room) in enumerate(room_list):
                room_name = _get_room_name(room)
                self._backfill_state["current_room"] = room_name
                self._backfill_state["processed_rooms"] = i
                logger.info("Backfill [%d/%d] %s", i + 1, len(room_list), room_name)
                try:
                    count = await backfill_room(client, self._store, room_id, room_name, args)
                    total_saved += count
                    self._backfill_state["saved"] = total_saved
                except Exception as e:
                    logger.error("Backfill failed for %s: %s", room_name, e, exc_info=True)

            self._backfill_state["processed_rooms"] = len(room_list)
            self._backfill_state["current_room"] = ""

            logger.info("Backfill: reconciling edits")
            await asyncio.to_thread(self._store.reconcile_edits)

            try:
                await state.save_sync_token("source", client.next_batch)
                await state.flush()
            except Exception:
                pass

            self._backfill_state["done"] = True
            self._backfill_state["running"] = False
            logger.info("Backfill complete: %d messages saved across %d rooms", total_saved, len(room_list))
        except Exception as e:
            logger.error("Backfill failed: %s", e, exc_info=True)
            self._backfill_state["error"] = str(e)
            self._backfill_state["running"] = False
            self._backfill_state["done"] = True
        finally:
            if client:
                try:
                    await client.close()
                except Exception:
                    pass

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info("Web server started on %s:%s", self._host, self._port)

    @staticmethod
    def _clear_media_dir(media_dir: str) -> None:
        for entry in os.listdir(media_dir):
            p = os.path.join(media_dir, entry)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.unlink(p)
            except OSError:
                pass
        logger.info("Backfill: media directory cleared")

    async def stop(self) -> None:
        if hasattr(self, "_runner"):
            await self._runner.cleanup()
            logger.info("Web server stopped")
