#!/usr/bin/env python
"""Run the real Telegram loop locally (dev only).

Starts an ngrok tunnel to the local intake API, registers the Telegram webhook
with a one-shot secret token, and serves the intake endpoint so a message from
your phone round-trips through the walking-skeleton orchestrator pipeline
(routing + session state + a reply) back to you. Cleans up (``deleteWebhook`` +
tunnel shutdown) on exit.

Usage (from the repo root):
    .venv/bin/python scripts/telegram_loop.py [--port N]

Prerequisites:
    - ``TELEGRAM_BOT_TOKEN`` set in ``.env``.
    - ``ngrok`` installed AND authed (``ngrok config add-authtoken <token>``).
    - ``FASE2_ENABLED=true`` (the default) so the webhook dispatches the handler.

This exercises the same ``/webhook/telegram`` endpoint, ``TelegramChannel``
adapter and orchestrator routing as production, but with stub agents (no
OpenAI/Postgres needed).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# The secret token must be in the environment BEFORE the app imports, because
# pydantic-settings caches Settings() and env vars win over the .env file. This
# keeps setWebhook and the verification path on the same one-shot token.
os.environ["TELEGRAM_SECRET_TOKEN"] = secrets.token_urlsafe(24)

import uvicorn

from src.api import webhook as webhook_module
from src.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("telegram-loop")

DEFAULT_PORT = 8000
NGROK_API = "http://127.0.0.1:4040/api/tunnels"


def _api_url(method: str) -> str:
    token = get_settings().telegram_bot_token
    return f"https://api.telegram.org/bot{token}/{method}"


def _tg_call(method: str, data: dict[str, str]) -> dict[str, object]:
    """Synchronous POST to the Telegram Bot API (form-encoded)."""
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(_api_url(method), data=body)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode())


def _tail(path: str, lines: int = 20) -> str:
    """Return the last ``lines`` lines of a file (to surface ngrok errors)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:]).strip()
    except OSError:
        return ""


def _wait_for_public_url(ngrok: subprocess.Popen, log_path: str, timeout: float = 30.0) -> str:
    """Poll ngrok's local API for a public HTTPS URL, surfacing ngrok errors."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ngrok.poll() is not None:
            raise RuntimeError(f"ngrok exited with code {ngrok.returncode}:\n{_tail(log_path)}")
        try:
            with urllib.request.urlopen(NGROK_API, timeout=2) as response:
                payload = json.loads(response.read().decode())
        except (OSError, json.JSONDecodeError):
            time.sleep(0.5)
            continue
        urls = [str(t.get("public_url", "")) for t in payload.get("tunnels") or []]
        https = next((u for u in urls if u.startswith("https://")), None)
        if https:
            return https
        time.sleep(0.5)
    raise RuntimeError(f"ngrok tunnel did not come up in time:\n{_tail(log_path)}")


def main(port: int) -> int:
    settings = get_settings()
    if not settings.telegram_bot_token:
        logger.error("TELEGRAM_BOT_TOKEN is not set in .env")
        return 1

    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as log_file:
        ngrok = subprocess.Popen(
            ["ngrok", "http", str(port)],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        log_path = log_file.name
    try:
        public_url = _wait_for_public_url(ngrok, log_path)
    except RuntimeError as exc:
        logger.error("%s", exc)
        if ngrok.poll() is None:
            ngrok.terminate()
        os.unlink(log_path)
        return 1

    webhook_url = f"{public_url}/webhook/telegram"
    try:
        result = _tg_call(
            "setWebhook",
            {"url": webhook_url, "secret_token": settings.telegram_secret_token},
        )
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("setWebhook failed: %s", exc)
        ngrok.terminate()
        os.unlink(log_path)
        return 1
    if not result.get("ok"):
        logger.error("setWebhook rejected: %s", result)
        ngrok.terminate()
        os.unlink(log_path)
        return 1

    logger.info("loop ready — message your bot on Telegram")
    logger.info("webhook URL: %s", webhook_url)
    try:
        uvicorn.run(webhook_module.app, host="127.0.0.1", port=port, log_level="info")
    finally:
        logger.info("shutting down: deleteWebhook + stopping ngrok")
        try:
            _tg_call("deleteWebhook", {"drop_pending_updates": "true"})
        except (OSError, json.JSONDecodeError):
            logger.exception("deleteWebhook failed")
        if ngrok.poll() is None:
            ngrok.terminate()
        ngrok.wait(timeout=10)
        try:
            os.unlink(log_path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the local Telegram loop (ngrok + webhook + echo)."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"local port to serve the API and tunnel (default: {DEFAULT_PORT})",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    sys.exit(main(args.port))
