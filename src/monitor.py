"""Main daemon loop for Goethe booking monitoring."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from browser import InteractionStep, fetch_page
from detector import DEFAULT_AVAILABLE_SELECTORS, DEFAULT_BOOKED_SELECTORS, detect_slot
from notifier import TelegramNotifier
from state import StateStore


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
SCREENSHOT_DIR = LOG_DIR / "screenshots"
STATE_PATH = LOG_DIR / "state.json"
CONFIG_PATH = PROJECT_ROOT / "config" / "targets.json"


class KeyValueFormatter(logging.Formatter):
    """Simple structured formatter."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log records as key=value pairs."""

        base = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key
            not in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "message",
                "asctime",
            }
        }
        payload = {**base, **extras}
        return " ".join(f"{key}={json.dumps(value, ensure_ascii=True)}" for key, value in payload.items())


def configure_logging() -> logging.Logger:
    """Configure rotating file and console logging."""

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = KeyValueFormatter()
    root = logging.getLogger("goethe_sentry")
    root.setLevel(logging.INFO)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        LOG_DIR / "sentry.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root.addHandler(console)
    root.addHandler(file_handler)
    root.propagate = False
    return root


def load_config(path: Path) -> dict[str, Any]:
    """Load and minimally validate target configuration."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if "targets" not in payload or not isinstance(payload["targets"], list):
        raise ValueError("config/targets.json must include a targets list")
    return payload


def active_targets(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only enabled targets."""

    return [target for target in config["targets"] if target.get("enabled", True)]


def build_required_selectors(target: dict[str, Any]) -> list[str]:
    """Build the combined selector list required for browser extraction."""

    available = target.get("available_selectors") or DEFAULT_AVAILABLE_SELECTORS
    booked = target.get("booked_selectors") or DEFAULT_BOOKED_SELECTORS
    return list(dict.fromkeys([*available, *booked]))


def build_wait_selectors(target: dict[str, Any]) -> list[str]:
    """Return wait selectors for a target page."""

    selectors = target.get("wait_for_selectors", [])
    return [selector for selector in selectors if selector]


def build_interaction_steps(target: dict[str, Any]) -> list[InteractionStep]:
    """Build browser interaction steps from target config."""

    steps: list[InteractionStep] = []
    for raw_step in target.get("interaction_steps", []):
        steps.append(
            InteractionStep(
                action=str(raw_step["action"]),
                selector=str(raw_step["selector"]),
                value=raw_step.get("value"),
                wait_for_request_contains=raw_step.get("wait_for_request_contains"),
                wait_for_selectors=list(raw_step.get("wait_for_selectors", [])),
                post_delay_ms=int(raw_step.get("post_delay_ms", 0)),
            )
        )
    return steps


def env_or_raise(name: str) -> str:
    """Load a required environment variable."""

    import os

    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def build_notifier(logger: logging.Logger) -> TelegramNotifier:
    """Construct a notifier from environment variables."""

    return TelegramNotifier(
        bot_token=env_or_raise("TELEGRAM_BOT_TOKEN"),
        chat_id=env_or_raise("TELEGRAM_CHAT_ID"),
        logger=logger,
    )


def should_rate_limit(last_alert_sent: str | None, window_seconds: int = 300) -> bool:
    """Return true when the last alert is within the anti-spam window."""

    if not last_alert_sent:
        return False
    last_dt = datetime.fromisoformat(last_alert_sent)
    return datetime.now(timezone.utc) - last_dt < timedelta(seconds=window_seconds)


async def process_target(
    target: dict[str, Any],
    state_store: StateStore,
    notifier: TelegramNotifier,
    logger: logging.Logger,
) -> None:
    """Run one monitoring cycle for a single target."""

    label = target["label"]
    selectors = build_required_selectors(target)
    result = await fetch_page(
        url=target["url"],
        screenshot_dir=SCREENSHOT_DIR / label.replace(" ", "_"),
        selectors=selectors,
        logger=logger,
        interaction_steps=build_interaction_steps(target),
        system=target.get("system"),
        course_keywords=list(target.get("course_keywords", [])),
        detail_probe_max_batches=int(target.get("detail_probe_max_batches", 0)),
        wait_for_selectors=build_wait_selectors(target),
        wait_timeout_ms=int(target.get("wait_timeout_ms", 12_000)),
    )
    detection = detect_slot(result, target, logger)
    update = await state_store.update_target_state(
        label=label,
        status=detection.status,
        signature=detection.signature,
        detail_recovered=detection.detail_recovered,
    )

    logger.info(
        "target_processed",
        extra={
            "label": label,
            "status": detection.status,
            "method": detection.method,
            "confidence": detection.confidence,
            "matched": detection.matched,
            "error": result.error,
            "load_time_ms": result.load_time_ms,
        },
    )

    if update.warning_threshold_crossed:
        await notifier.send_alert(
            f"⚠️ Detection warning — {label}\n"
            "This target has returned unknown results more than 5 times in a row.\n"
            f"Latest screenshot: {result.screenshot_path}"
        )

    if detection.detail_recovered and not update.previous_detail_recovered:
        recovery_lines = [
            f"✅ Detail Endpoint Recovered — {label}",
            "Batch detail enrichment is returning usable data again.",
            f"⏰ Detected: {datetime.now(timezone.utc).isoformat()}",
        ]
        if detection.summary_lines:
            recovery_lines.extend(detection.summary_lines[:5])
        await notifier.send_alert("\n".join(recovery_lines), photo_path=result.screenshot_path)
        logger.info("detail_endpoint_recovered", extra={"label": label})

    if detection.status != "available":
        return

    if should_rate_limit(update.state.get("last_alert_sent")):
        logger.info("alert_rate_limited", extra={"label": label})
        return

    if update.previous_status == "available" and update.previous_signature == detection.signature:
        logger.info("alert_skipped_same_state", extra={"label": label})
        return

    timestamp = datetime.now(timezone.utc).isoformat()
    lines = [
        f"🚨 SLOT AVAILABLE — {label}\n"
        "📅 Exam slot just opened!\n"
        f"🔗 Book NOW: {target['url']}\n"
        f"⏰ Detected: {timestamp}\n"
        f"🔍 Method: {detection.method}\n"
        f"📊 Confidence: {detection.confidence}"
    ]
    if detection.summary_lines:
        lines.extend(detection.summary_lines[:5])
    message = "\n".join(lines)
    await notifier.send_alert(message, photo_path=result.screenshot_path)
    await state_store.mark_alert_sent(label)


async def run_once(config: dict[str, Any], logger: logging.Logger) -> None:
    """Run a single monitoring pass across all enabled targets and exit."""

    notifier = build_notifier(logger)
    state_store = StateStore(STATE_PATH)
    targets = active_targets(config)
    logger.info("one_shot_started", extra={"targets": [target["label"] for target in targets]})
    for target in targets:
        try:
            await process_target(target, state_store, notifier, logger)
        except Exception as exc:
            logger.exception("target_cycle_failed", extra={"label": target["label"], "error": str(exc)})


async def run_daemon(config: dict[str, Any], logger: logging.Logger) -> None:
    """Run the main monitoring loop until interrupted."""

    import contextlib

    targets = active_targets(config)
    notifier = build_notifier(logger)
    state_store = StateStore(STATE_PATH)
    started_at = time.monotonic()
    await notifier.send_startup(targets)
    logger.info("daemon_started", extra={"targets": [target["label"] for target in targets]})

    last_heartbeat = time.monotonic()
    heartbeat_interval = int(config.get("heartbeat_interval_hours", 6)) * 3600
    poll_min = int(config.get("poll_min_seconds", 90))
    poll_max = int(config.get("poll_max_seconds", 180))

    try:
        while True:
            for target in targets:
                try:
                    await process_target(target, state_store, notifier, logger)
                except Exception as exc:
                    logger.exception("target_cycle_failed", extra={"label": target["label"], "error": str(exc)})

            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_interval:
                states = await state_store.load()
                uptime_hours = int((now - started_at) // 3600)
                await notifier.send_heartbeat(targets, states, uptime_hours)
                last_heartbeat = now

            delay = random.randint(poll_min, poll_max)
            logger.info("sleeping_before_next_cycle", extra={"seconds": delay})
            await asyncio.sleep(delay)
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
        with contextlib.suppress(Exception):
            await notifier.send_shutdown()
        raise


async def run_check(config: dict[str, Any], logger: logging.Logger) -> None:
    """Validate imports, environment loading, and configuration without monitoring."""

    targets = active_targets(config)
    await StateStore(STATE_PATH).load()
    logger.info(
        "startup_check_ok",
        extra={
            "targets": [target["label"] for target in targets],
            "poll_min_seconds": config.get("poll_min_seconds"),
            "poll_max_seconds": config.get("poll_max_seconds"),
            "heartbeat_interval_hours": config.get("heartbeat_interval_hours"),
        },
    )
    print(f"Startup check OK. Loaded {len(targets)} active target(s).")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(description="Goethe booking sentry")
    parser.add_argument("--check", action="store_true", help="Validate config and imports, then exit")
    return parser.parse_args()


async def async_main() -> int:
    """Entrypoint for async execution."""

    load_dotenv(PROJECT_ROOT / ".env")
    logger = configure_logging()
    config = load_config(CONFIG_PATH)
    args = parse_args()
    if args.check:
        await run_check(config, logger)
        return 0
    try:
        await run_daemon(config, logger)
    except KeyboardInterrupt:
        return 0
    return 0


async def async_run_once() -> int:
    """Entrypoint used by one-shot runners such as GitHub Actions."""

    load_dotenv(PROJECT_ROOT / ".env")
    logger = configure_logging()
    config = load_config(CONFIG_PATH)
    await run_once(config, logger)
    return 0


def main() -> int:
    """Synchronous wrapper for asyncio execution."""

    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
