"""
telegram_bot.py — standalone Telegram Bot API client.

Separated from nox_server.py so the bot logic can be developed,
tested, or extended independently of the HTTP server.

Usage (from a Python script or the REPL):

    from telegram_bot import TelegramBot
    bot = TelegramBot()               # reads token/chat_id from env
    bot.send("Hello from the server!")
    bot.send_form_submission(form_data)

Environment variables required:
    TELEGRAM_BOT_TOKEN  — from @BotFather
    TELEGRAM_CHAT_ID    — your personal chat ID (from /getUpdates)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional


class TelegramBot:
    """
    Thin wrapper around the Telegram Bot API.
    All methods make a single HTTPS POST on port 443 — no SMTP,
    no external packages, just urllib from the standard library.
    """

    BASE_URL = "https://api.telegram.org"

    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")

        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
        if not self.chat_id:
            raise RuntimeError("TELEGRAM_CHAT_ID not set")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def send(self, text: str, parse_mode: str = "Markdown") -> dict:
        """Send a plain text message to the configured chat."""
        return self._post(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
            },
        )

    def send_form_submission(self, form_data: dict[str, str]) -> dict:
        """
        Formats and sends a contact form submission as a Telegram message.
        Called by nox_server.send_telegram_notification() — lives here so
        the formatting logic is co-located with the bot client.
        """
        name = form_data.get("name", "Unknown")
        email = form_data.get("email", "Unknown")
        message = form_data.get("message", "")

        services = [
            label
            for key, label in [
                ("frontend", "Frontend"),
                ("webDevelopment", "Web Development"),
                ("blender", "Blender"),
            ]
            if form_data.get(key) == "on"
        ]
        services_line = f"🛠 *Services:* {', '.join(services)}" if services else ""

        text = (
            f"📬 *New Contact Form Submission*\n\n"
            f"👤 *Name:* {name}\n"
            f"📧 *Email:* {email}\n"
            f"💬 *Message:*\n{message}"
            + (f"\n\n{services_line}" if services_line else "")
        )

        return self.send(text)

    # ------------------------------------------------------------------
    # Low-level
    # ------------------------------------------------------------------
    def _post(self, method: str, payload: dict[str, Any]) -> dict:
        """
        POST to api.telegram.org/bot{token}/{method}.
        Returns the parsed JSON response dict on success.
        Raises RuntimeError with the API error message on failure.
        """
        url = f"{self.BASE_URL}/bot{self.token}/{method}"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"Telegram API error {e.code}: {body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Telegram request failed: {e.reason}")


# ------------------------------------------------------------------
# Quick smoke-test: python3 telegram_bot.py
# ------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    # Load .env if present so you can test locally without exporting vars
    try:
        from main import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    try:
        bot = TelegramBot()
        result = bot.send("🤖 *nox server bot is online and reachable.*")
        print("Sent successfully:", result)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
