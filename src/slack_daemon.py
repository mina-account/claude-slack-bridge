"""
slack_daemon.py — Slack Socket Mode listener + Unix domain socket server.

The daemon holds exactly one Socket Mode WebSocket connection to Slack and
accepts local connections from session processes (started via docker exec).

Each session connects, sends ``REGISTER {thread_ts}\n``, and blocks. When a
Slack reply arrives for that thread_ts the daemon forwards it over the socket,
unblocking the waiting session with zero polling.

Additionally, the daemon handles Human→Claude messages: top-level Slack
messages (and threaded replies with no pending MCP session) are forwarded to
the Claude Code CLI, and the response is posted back as a thread reply.
"""

import asyncio
import json
import logging
import os
import socket
from typing import Any

from aiohttp import web
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from claude_handler import ClaudeHandler

logger = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/slack-bridge.sock"
SLACK_MAX_MESSAGE_LENGTH = 40000


class SlackDaemon:
    """
    Bridges Slack Socket Mode events to waiting session processes via a
    Unix domain socket, and handles Human→Claude messages via the Claude
    Code CLI.

    Args:
        bot_token: Slack bot OAuth token (xoxb-...).
        app_token: Slack app-level token for Socket Mode (xapp-...).
    """

    def __init__(self, bot_token: str, app_token: str, http_port: int = 3409) -> None:
        self._app = AsyncApp(token=bot_token)
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        self._pending: dict[str, asyncio.StreamWriter] = {}
        self._lock = asyncio.Lock()
        self._claude = ClaudeHandler(slack_client=self._app.client)
        self._active_threads: set[str] = set()
        self._bot_user_id: str = ""
        self._http_port = http_port

        self._app.event("message")(self._handle_slack_message)

    async def _handle_slack_message(self, event: dict[str, Any]) -> None:
        # Filter: Ignore bot messages (prevents self-echo loops).
        if event.get("bot_id"):
            return

        thread_ts: str | None = event.get("thread_ts")
        text: str = event.get("text", "")
        channel: str = event.get("channel", "")

        # Case 1: Threaded reply WITH a pending MCP session — forward to session.
        if thread_ts:
            async with self._lock:
                writer = self._pending.pop(thread_ts, None)

            if writer is not None:
                logger.info("Slack reply in thread %s: %r", thread_ts, text)
                try:
                    writer.write(text.encode() + b"\n")
                    await writer.drain()
                    logger.info("Reply forwarded to session for thread %s.", thread_ts)
                except Exception as exc:
                    logger.warning("Failed to forward reply for %s: %s", thread_ts, exc)
                finally:
                    writer.close()
                return

        # Case 2: Threaded reply with NO pending session — continue Claude conversation.
        if thread_ts:
            if thread_ts in self._active_threads:
                return
            asyncio.create_task(self._handle_claude_thread_reply(channel, thread_ts, text))
            return

        # Case 3: Top-level message — only respond if the bot is mentioned.
        mention_tag = f"<@{self._bot_user_id}>"
        if mention_tag not in text:
            return

        # Strip the mention from the text so Claude sees clean input.
        text = text.replace(mention_tag, "").strip()

        message_ts: str = event.get("ts", "")
        if message_ts in self._active_threads:
            return
        asyncio.create_task(self._handle_claude_new_message(channel, message_ts, text))

    async def _handle_claude_new_message(self, channel: str, message_ts: str, text: str) -> None:
        """Spawn Claude for a new top-level message and post the response as a thread reply."""
        self._active_threads.add(message_ts)
        try:
            response = await self._claude.handle_message(channel, message_ts, text)
            await self._post_response(channel, message_ts, response)
        except Exception as exc:
            logger.error("Error handling top-level message %s: %s", message_ts, exc)
        finally:
            self._active_threads.discard(message_ts)

    async def _handle_claude_thread_reply(self, channel: str, thread_ts: str, text: str) -> None:
        """Spawn Claude for a thread reply and post the response."""
        self._active_threads.add(thread_ts)
        try:
            response = await self._claude.handle_thread_reply(channel, thread_ts, text)
            await self._post_response(channel, thread_ts, response)
        except Exception as exc:
            logger.error("Error in thread continuation %s: %s", thread_ts, exc)
        finally:
            self._active_threads.discard(thread_ts)

    async def _post_response(self, channel: str, thread_ts: str, text: str) -> None:
        """Post a response to Slack, splitting if it exceeds the message length limit."""
        if len(text) <= SLACK_MAX_MESSAGE_LENGTH:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text, mrkdwn=True,
            )
            return

        for i in range(0, len(text), SLACK_MAX_MESSAGE_LENGTH):
            chunk = text[i : i + SLACK_MAX_MESSAGE_LENGTH]
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=chunk, mrkdwn=True,
            )

    async def _handle_session_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        thread_ts: str | None = None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            parts = line.decode().strip().split(" ", 1)

            if len(parts) != 2 or parts[0] != "REGISTER":
                logger.warning("Bad session registration: %r", line)
                return

            thread_ts = parts[1]
            async with self._lock:
                self._pending[thread_ts] = writer

            logger.info("Session registered for thread %s.", thread_ts)

            # Block until the session disconnects (reader.read returns b"" on close).
            # This ensures _pending is cleaned up if the session exits before a reply arrives.
            await reader.read(1)

        except Exception as exc:
            logger.error("Session connection error: %s", exc)
        finally:
            if thread_ts:
                async with self._lock:
                    self._pending.pop(thread_ts, None)
            if not writer.is_closing():
                writer.close()

    async def _handle_http_callback(self, request: web.Request) -> web.Response:
        """
        POST /callback — external app notifies Claude with a result for an existing thread.

        Expected JSON body:
            { "thread_ts": "<slack thread timestamp>", "text": "<message to pass to Claude>" }

        Claude will respond in the same Slack thread.
        """
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON body.")

        thread_ts = body.get("thread_ts")
        text = body.get("text")

        if not thread_ts or not text:
            return web.Response(status=400, text="Missing required fields: thread_ts, text.")

        channel = self._claude.get_channel_for_thread(thread_ts)
        if not channel:
            return web.Response(status=404, text=f"Unknown thread_ts: {thread_ts}. Session may have expired or never existed.")

        if thread_ts in self._active_threads:
            return web.Response(status=409, text="Thread is currently busy processing another message.")

        prefixed_text = f"[CALLBACK] {text}"
        logger.info("HTTP callback for thread %s: %r", thread_ts, prefixed_text)
        asyncio.create_task(self._handle_claude_thread_reply(channel, thread_ts, prefixed_text))
        return web.Response(status=202, text="Accepted.")

    async def start(self) -> None:
        """Start the Unix socket server, HTTP callback server, and Slack Socket Mode handler."""
        await self._claude.initialize()
        self._bot_user_id = self._claude._bot_user_id

        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

        unix_server = await asyncio.start_unix_server(
            self._handle_session_connection, path=SOCKET_PATH
        )
        logger.info("Unix socket server listening at %s.", SOCKET_PATH)

        http_app = web.Application()
        http_app.router.add_post("/callback", self._handle_http_callback)
        runner = web.AppRunner(http_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._http_port)
        await site.start()

        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        callback_url = f"http://{local_ip}:{self._http_port}/callback"
        logger.info("HTTP callback endpoint: %s", callback_url)
        print(f"\n  Callback endpoint: POST {callback_url}\n")

        async with unix_server:
            await asyncio.gather(
                unix_server.serve_forever(),
                self._handler.start_async(),
            )
