"""Telegram notification helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


class TelegramNotifier:
    """Thin async Telegram Bot API client."""

    def __init__(self, bot_token: str, chat_id: str, logger: Any) -> None:
        """Initialize the notifier."""

        self.bot_token = bot_token
        self.chat_id = chat_id
        self.logger = logger
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self._timeout = httpx.Timeout(20.0)

    async def _post(self, endpoint: str, data: dict[str, Any], files: dict[str, Any] | None = None) -> None:
        """Send a request to Telegram with bounded retries."""

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for attempt in range(1, 4):
                try:
                    response = await client.post(f"{self.base_url}/{endpoint}", data=data, files=files)
                    response.raise_for_status()
                    payload = response.json()
                    if not payload.get("ok", False):
                        raise RuntimeError(f"Telegram API error: {payload}")
                    return
                except Exception as exc:
                    self.logger.warning(
                        "telegram_send_failed",
                        extra={"endpoint": endpoint, "attempt": attempt, "error": str(exc)},
                    )
                    if attempt >= 3:
                        raise
                    await asyncio.sleep(attempt)

    async def send_alert(self, message: str, photo_path: str | None = None) -> None:
        """Send an alert message, optionally with a photo."""

        if photo_path:
            photo_file = Path(photo_path)
            with photo_file.open("rb") as handle:
                await self._post(
                    "sendPhoto",
                    data={"chat_id": self.chat_id, "caption": message},
                    files={"photo": (photo_file.name, handle, "image/png")},
                )
            return
        await self._post("sendMessage", data={"chat_id": self.chat_id, "text": message})

    async def send_startup(self, targets: list[dict[str, Any]]) -> None:
        """Send a startup message listing active targets."""

        labels = ", ".join(target["label"] for target in targets if target.get("enabled", True))
        message = (
            "🛡️ Goethe Sentry started\n"
            f"⏰ {datetime.now(timezone.utc).isoformat()}\n"
            f"📍 Active targets: {labels or 'none'}"
        )
        await self.send_alert(message)

    async def send_shutdown(self) -> None:
        """Send a shutdown message."""

        message = (
            "🛑 Goethe Sentry stopped\n"
            f"⏰ {datetime.now(timezone.utc).isoformat()}\n"
            "Goodbye."
        )
        await self.send_alert(message)

    async def send_heartbeat(self, targets: list[dict[str, Any]], states: dict[str, dict[str, Any]], uptime_hours: int) -> None:
        """Send a heartbeat showing current target states."""

        lines = ["🛡️ Sentry alive", f"⏱ Running for {uptime_hours} hours"]
        for target in targets:
            if not target.get("enabled", True):
                continue
            state = states.get(target["label"], {})
            status = state.get("status", "unknown")
            lines.append(f"📍 {target['label']}: still {status}")
        await self.send_alert("\n".join(lines))
