"""Main daemon loop for Goethe booking monitoring."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from browser import CenterDiscoveryResult, InteractionStep, discover_partner_centers, fetch_page
from detector import DEFAULT_AVAILABLE_SELECTORS, DEFAULT_BOOKED_SELECTORS, detect_slot
from notifier import TelegramNotifier
from state import StateStore


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
SCREENSHOT_DIR = LOG_DIR / "screenshots"
STATE_PATH = LOG_DIR / "state.json"
CONFIG_PATH = PROJECT_ROOT / "config" / "targets.json"
FAILURE_ALERT_THRESHOLD = 3
TARGET_RETRY_BACKOFFS = (5, 15)


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
    level_name = os.getenv("SENTRY_LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))
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


def normalize_center_name(name: str) -> str:
    """Normalize center names for stable comparisons."""

    return "".join(ch.lower() for ch in name if ch.isalnum())


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


def _diagnostics_dir(label: str) -> Path:
    """Return the diagnostics directory for a target."""

    return LOG_DIR / "diagnostics" / label.replace(" ", "_")


def save_failure_diagnostics(
    label: str,
    result: Any | None,
    error_message: str,
    state_snapshot: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """Persist concise diagnostics for a failed target run."""

    diag_dir = _diagnostics_dir(label)
    diag_dir.mkdir(parents=True, exist_ok=True)
    html_path = diag_dir / "latest.html"
    json_path = diag_dir / "latest.json"
    if result and getattr(result, "raw_html", ""):
        html_path.write_text(result.raw_html, encoding="utf-8")
    payload = {
        "label": label,
        "error": error_message,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "screenshot_path": getattr(result, "screenshot_path", None) if result else None,
        "load_time_ms": getattr(result, "load_time_ms", None) if result else None,
        "page_title": getattr(result, "page_title", None) if result else None,
        "state": state_snapshot,
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("failure_diagnostics_saved", extra={"label": label, "json_path": str(json_path), "html_path": str(html_path)})


def save_discovery_diagnostics(
    label: str,
    result: CenterDiscoveryResult,
    summary: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """Persist discovery diagnostics for failed or partial center discovery."""

    diag_dir = _diagnostics_dir(label)
    diag_dir.mkdir(parents=True, exist_ok=True)
    (diag_dir / "centers_latest.html").write_text(result.raw_html, encoding="utf-8")
    (diag_dir / "centers_latest.txt").write_text(result.visible_text, encoding="utf-8")
    (diag_dir / "centers_latest.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("center_discovery_diagnostics_saved", extra={"label": label, "diag_dir": str(diag_dir)})


def resolve_center_selection(
    discovered_names: list[str],
    discovery_cfg: dict[str, Any],
) -> list[str]:
    """Resolve which discovered/configured centers should be monitored for this run."""

    scope = os.getenv("SENTRY_CENTER_SCOPE", "").strip().lower()
    selected_env = [item.strip() for item in os.getenv("SENTRY_SELECTED_CENTERS", "").split(",") if item.strip()]
    if scope == "selected" and selected_env:
        return selected_env

    config_override = list(discovery_cfg.get("allowlist_override", []))
    if config_override:
        return config_override

    if scope == "pinned":
        return list(discovery_cfg.get("pinned_centers", []))

    include_centers = discovery_cfg.get("include_centers", "all")
    if include_centers == "all":
        return discovered_names
    if isinstance(include_centers, list):
        return list(include_centers)
    return discovered_names


async def resolve_targets(
    config: dict[str, Any],
    state_store: StateStore,
    notifier: TelegramNotifier,
    logger: logging.Logger,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve enabled targets, expanding partner-center discovery templates."""

    resolved_targets: list[dict[str, Any]] = []
    run_summary: dict[str, Any] = {
        "checked_centers": [],
        "missing_on_site": [],
        "newly_added_centers": [],
        "removed_centers": [],
        "could_not_parse_centers": [],
    }
    for target in active_targets(config):
        discovery_cfg = target.get("center_discovery")
        if not discovery_cfg:
            resolved_targets.append(target)
            continue

        discovery_result = await discover_partner_centers(
            url=target["url"],
            screenshot_dir=SCREENSHOT_DIR / "center_discovery",
            select_selector=discovery_cfg.get("selector", "#cmbExamCentre"),
            wait_for_selectors=build_wait_selectors(target),
            logger=logger,
            timeout_ms=int(target.get("page_timeout_ms", 45_000)),
        )
        aux_key = f"__center_discovery__::{target['label']}"
        discovered_centers = [
            center.name
            for center in discovery_result.centers
            if center.value and center.value != "0" and center.name and "select" not in center.name.lower()
        ]
        discovered_map = {normalize_center_name(center.name): center for center in discovery_result.centers if center.value and center.value != "0" and center.name}
        prev_summary = await state_store.update_aux_state(
            aux_key,
            {
                "discovered_centers": discovered_centers,
                "last_error": discovery_result.error,
                "last_run_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        prev_discovered = list(prev_summary.get("discovered_centers", []))
        selected_centers = resolve_center_selection(discovered_centers, discovery_cfg)
        pinned_centers = list(discovery_cfg.get("pinned_centers", []))
        expected_centers = sorted(set(selected_centers + pinned_centers))
        missing_on_site = [
            center for center in expected_centers if normalize_center_name(center) not in discovered_map
        ]
        newly_added = [center for center in discovered_centers if center not in prev_discovered]
        removed = [center for center in prev_discovered if center not in discovered_centers]
        run_summary["missing_on_site"].extend(missing_on_site)
        run_summary["newly_added_centers"].extend(newly_added)
        run_summary["removed_centers"].extend(removed)

        if discovery_result.error:
            run_summary["could_not_parse_centers"].append(target["label"])
            save_discovery_diagnostics(
                target["label"],
                discovery_result,
                {
                    "error": discovery_result.error,
                    "discovered_centers": discovered_centers,
                    "missing_on_site": missing_on_site,
                },
                logger,
            )
            logger.warning(
                "center_discovery_failed",
                extra={"label": target["label"], "error": discovery_result.error},
            )
            continue

        prev_missing = set(prev_summary.get("missing_on_site", []))
        await state_store.update_aux_state(
            aux_key,
            {
                "discovered_centers": discovered_centers,
                "missing_on_site": missing_on_site,
                "last_error": None,
                "last_run_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        for center_name in discovered_centers:
            if normalize_center_name(center_name) not in {normalize_center_name(item) for item in selected_centers}:
                continue
            center_info = discovered_map[normalize_center_name(center_name)]
            derived_target = copy.deepcopy(target)
            derived_target["label"] = f"{center_name} Goethe Partner"
            derived_target["resolved_center_name"] = center_name
            derived_target["center_value"] = center_info.value
            derived_target["interaction_steps"] = [
                {
                    "action": "select",
                    "selector": discovery_cfg.get("selector", "#cmbExamCentre"),
                    "value": center_info.value,
                    "wait_for_request_contains": f"/home-exams?cmbcentre={center_info.value}",
                    "wait_for_selectors": [".exam-batches .course-card"],
                    "post_delay_ms": 1500,
                }
            ]
            resolved_targets.append(derived_target)
            run_summary["checked_centers"].append(center_name)

            if center_name in pinned_centers and center_name in prev_missing:
                await notifier.send_alert(
                    "\n".join(
                        [
                            f"📍 Configured Center Discovered — {center_name}",
                            f"Source: {target['url']}",
                            f"⏰ Detected: {datetime.now(timezone.utc).isoformat()}",
                        ]
                    )
                )
                logger.info("configured_center_discovered", extra={"center": center_name})

        for center_name in discovered_centers:
            if center_name in prev_missing and center_name in pinned_centers:
                await notifier.send_alert(
                    "\n".join(
                        [
                            f"✅ Missing Center Recovered — {center_name}",
                            f"Source: {target['url']}",
                            f"⏰ Detected: {datetime.now(timezone.utc).isoformat()}",
                        ]
                    )
                )
                logger.info("missing_center_recovered", extra={"center": center_name})

        logger.info(
            "center_discovery_summary",
            extra={
                "label": target["label"],
                "country": discovery_cfg.get("country", "India"),
                "discovered_centers": discovered_centers,
                "selected_centers": selected_centers,
                "missing_on_site": missing_on_site,
                "newly_added_centers": newly_added,
                "removed_centers": removed,
            },
        )

    return resolved_targets, run_summary


async def process_target(
    target: dict[str, Any],
    state_store: StateStore,
    notifier: TelegramNotifier,
    logger: logging.Logger,
) -> dict[str, str]:
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
        timeout_ms=int(target.get("page_timeout_ms", 45_000)),
    )
    if result.error:
        update = await state_store.update_target_state(
            label=label,
            status="error",
            signature="error",
            detail_recovered=False,
            last_error=result.error,
            success=False,
        )
        logger.warning(
            "target_fetch_error",
            extra={"label": label, "error": result.error, "load_time_ms": result.load_time_ms},
        )
        save_failure_diagnostics(label, result, result.error, update.state, logger)
        if update.warning_threshold_crossed:
            await notifier.send_alert(
                f"⚠️ Repeated failures — {label}\n"
                f"Consecutive failures: {update.current_consecutive_failures}\n"
                f"Last error: {result.error}\n"
                f"⏰ Detected: {datetime.now(timezone.utc).isoformat()}"
            )
        return {"label": label, "status": "page_error"}

    detection = detect_slot(result, target, logger)
    update = await state_store.update_target_state(
        label=label,
        status=detection.status,
        signature=detection.signature,
        detail_recovered=detection.detail_recovered,
        last_error=None,
        success=True,
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

    recovered_from_error = update.previous_status == "error" and detection.status != "error"
    if update.previous_status == "error" and detection.status != "error":
        await notifier.send_alert(
            "\n".join(
                [
                    f"✅ Recovered From Error — {label}",
                    f"Status: {detection.status}",
                    f"⏰ Detected: {datetime.now(timezone.utc).isoformat()}",
                ]
            ),
            photo_path=result.screenshot_path,
        )
        logger.info("target_recovered_from_error", extra={"label": label, "status": detection.status})

    if update.previous_status == "available" and detection.status != "available":
        await notifier.send_alert(
            "\n".join(
                [
                    f"ℹ️ Availability Changed — {label}",
                    f"Status is now: {detection.status}",
                    f"⏰ Detected: {datetime.now(timezone.utc).isoformat()}",
                ]
            ),
            photo_path=result.screenshot_path,
        )
        logger.info("availability_changed", extra={"label": label, "status": detection.status})

    if detection.status != "available":
        return {"label": label, "status": detection.status}

    if recovered_from_error:
        logger.info("availability_alert_skipped_recovery", extra={"label": label})
        return {"label": label, "status": detection.status}

    if should_rate_limit(update.state.get("last_alert_sent")):
        logger.info("alert_rate_limited", extra={"label": label})
        return {"label": label, "status": detection.status}

    if update.previous_status == "available" and update.previous_signature == detection.signature:
        logger.info("alert_skipped_same_state", extra={"label": label})
        return {"label": label, "status": detection.status}

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
    return {"label": label, "status": detection.status}


async def run_target_with_retries(
    target: dict[str, Any],
    state_store: StateStore,
    notifier: TelegramNotifier,
    logger: logging.Logger,
) -> dict[str, str]:
    """Run one target with bounded retries for transient exceptions."""

    label = target["label"]
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            return await process_target(target, state_store, notifier, logger)
        except Exception as exc:
            is_last_attempt = attempt >= max_retries
            logger.exception(
                "target_cycle_failed",
                extra={"label": label, "error": str(exc), "attempt": attempt + 1},
            )
            if is_last_attempt:
                update = await state_store.update_target_state(
                    label=label,
                    status="error",
                    signature="exception",
                    detail_recovered=False,
                    last_error=str(exc),
                    success=False,
                )
                save_failure_diagnostics(label, None, str(exc), update.state, logger)
                if update.warning_threshold_crossed:
                    await notifier.send_alert(
                        f"⚠️ Repeated failures — {label}\n"
                        f"Consecutive failures: {update.current_consecutive_failures}\n"
                        f"Last error: {exc}\n"
                        f"⏰ Detected: {datetime.now(timezone.utc).isoformat()}"
                    )
                return {"label": label, "status": "extraction_failed"}
            backoff_seconds = TARGET_RETRY_BACKOFFS[min(attempt, len(TARGET_RETRY_BACKOFFS) - 1)]
            logger.info(
                "target_retry_backoff",
                extra={"label": label, "attempt": attempt + 1, "backoff_seconds": backoff_seconds},
            )
            await asyncio.sleep(backoff_seconds)
    return {"label": label, "status": "extraction_failed"}


async def run_once(config: dict[str, Any], logger: logging.Logger) -> None:
    """Run a single monitoring pass across all enabled targets and exit."""

    notifier = build_notifier(logger)
    state_store = StateStore(STATE_PATH)
    targets, discovery_summary = await resolve_targets(config, state_store, notifier, logger)
    logger.info("one_shot_started", extra={"targets": [target["label"] for target in targets]})
    city_results: list[dict[str, str]] = []
    for target in targets:
        city_results.append(await run_target_with_retries(target, state_store, notifier, logger))
    logger.info(
        "run_summary",
        extra={
            "checked_centers": discovery_summary["checked_centers"],
            "missing_on_site": discovery_summary["missing_on_site"],
            "could_not_parse_centers": discovery_summary["could_not_parse_centers"],
            "newly_added_centers": discovery_summary["newly_added_centers"],
            "removed_centers": discovery_summary["removed_centers"],
            "city_results": city_results,
        },
    )


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
            targets, discovery_summary = await resolve_targets(config, state_store, notifier, logger)
            city_results: list[dict[str, str]] = []
            for target in targets:
                city_results.append(await run_target_with_retries(target, state_store, notifier, logger))
            logger.info(
                "run_summary",
                extra={
                    "checked_centers": discovery_summary["checked_centers"],
                    "missing_on_site": discovery_summary["missing_on_site"],
                    "could_not_parse_centers": discovery_summary["could_not_parse_centers"],
                    "newly_added_centers": discovery_summary["newly_added_centers"],
                    "removed_centers": discovery_summary["removed_centers"],
                    "city_results": city_results,
                },
            )

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
