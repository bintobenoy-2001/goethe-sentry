"""Async Playwright browser helpers for fetching booking pages."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright


USER_AGENTS: list[str] = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.224 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.130 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_7_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.6045.199 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


@dataclass(slots=True)
class ButtonInfo:
    """Normalized information for a clickable element."""

    text: str
    classes: list[str]
    disabled: bool
    aria_label: str | None


@dataclass(slots=True)
class LinkInfo:
    """Normalized information for a link on the page."""

    text: str
    href: str


@dataclass(slots=True)
class PageResult:
    """Full page extraction result used by the detector."""

    raw_html: str
    visible_text: str
    buttons: list[ButtonInfo]
    links: list[LinkInfo]
    screenshot_path: str
    page_title: str
    load_time_ms: int
    error: str | None
    selector_matches: dict[str, int] = field(default_factory=dict)
    exam_cards: list["CourseCardInfo"] = field(default_factory=list)


@dataclass(slots=True)
class InteractionStep:
    """A config-driven page interaction to perform before extraction."""

    action: str
    selector: str
    value: str | None = None
    wait_for_request_contains: str | None = None
    wait_for_selectors: list[str] = field(default_factory=list)
    post_delay_ms: int = 0


@dataclass(slots=True)
class CourseCardInfo:
    """Structured exam-card data extracted from partner portals."""

    title: str
    text: str
    batch_options: list[str]
    exam_dates_text: str
    registration_dates_text: str
    external_fee_text: str
    internal_fee_text: str
    external_seats_text: str
    internal_seats_text: str
    has_external_register: bool
    has_internal_register: bool
    is_blocked: bool
    batch_details: list["BatchDetailInfo"] = field(default_factory=list)


@dataclass(slots=True)
class BatchDetailInfo:
    """Detailed batch information gathered from the batch-detail endpoint."""

    batch_id: str
    label: str
    response_status: int | None
    error: str | None
    exam_dates_text: str
    registration_dates_text: str
    external_fee_text: str
    internal_fee_text: str
    external_seats_text: str
    internal_seats_text: str


@dataclass(slots=True)
class CenterInfo:
    """Center option extracted from a dropdown or similar page source."""

    value: str
    name: str


@dataclass(slots=True)
class CenterDiscoveryResult:
    """Discovery result for partner-portal centers."""

    centers: list[CenterInfo]
    raw_html: str
    visible_text: str
    screenshot_path: str
    page_title: str
    load_time_ms: int
    error: str | None


SCREENSHOT_TIMEOUT_MS = 5_000


def _random_viewport() -> dict[str, int]:
    """Return a realistic randomized viewport size."""

    return {
        "width": random.randint(1280, 1680),
        "height": random.randint(720, 1080),
    }


async def _apply_stealth(context: BrowserContext) -> None:
    """Inject basic anti-fingerprinting scripts before page creation."""

    await context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['de-DE', 'de', 'en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4] });
        window.chrome = { runtime: {} };
        """
    )


async def _humanize_page(page: Page) -> None:
    """Perform small human-like interactions after load."""

    viewport = page.viewport_size or {"width": 1366, "height": 900}
    width = viewport["width"]
    height = viewport["height"]
    await page.mouse.move(
        random.randint(50, width // 2),
        random.randint(80, height // 2),
        steps=random.randint(12, 30),
    )
    await asyncio.sleep(random.uniform(0.2, 0.8))
    await page.mouse.wheel(0, random.randint(180, 420))
    await asyncio.sleep(random.uniform(0.2, 0.8))
    await page.mouse.move(
        random.randint(width // 3, width - 50),
        random.randint(height // 3, height - 50),
        steps=random.randint(10, 24),
    )
    await page.mouse.wheel(0, -random.randint(120, 280))


async def _extract_buttons(page: Page) -> list[ButtonInfo]:
    """Extract normalized button and button-like anchor metadata."""

    payload: list[dict[str, Any]] = await page.evaluate(
        """
        () => {
          const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
          return nodes.map((node) => ({
            text: (node.innerText || node.textContent || '').trim(),
            classes: Array.from(node.classList || []),
            disabled: node.hasAttribute('disabled') || node.getAttribute('aria-disabled') === 'true',
            aria_label: node.getAttribute('aria-label'),
          }));
        }
        """
    )
    return [
        ButtonInfo(
            text=item.get("text", ""),
            classes=list(item.get("classes", [])),
            disabled=bool(item.get("disabled", False)),
            aria_label=item.get("aria_label"),
        )
        for item in payload
    ]


async def _extract_links(page: Page) -> list[LinkInfo]:
    """Extract normalized link metadata."""

    payload: list[dict[str, Any]] = await page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href]')).map((node) => ({
          text: (node.innerText || node.textContent || '').trim(),
          href: node.href || '',
        }))
        """
    )
    return [
        LinkInfo(text=item.get("text", ""), href=item.get("href", ""))
        for item in payload
        if item.get("href")
    ]


async def _extract_selector_matches(page: Page, selectors: list[str]) -> dict[str, int]:
    """Count matching nodes for each selector, ignoring invalid selectors."""

    matches: dict[str, int] = {}
    for selector in selectors:
        if not selector:
            continue
        try:
            matches[selector] = await page.locator(selector).count()
        except Exception:
            matches[selector] = 0
    return matches


async def _extract_select_options(page: Page, selector: str) -> list[CenterInfo]:
    """Extract value/text pairs from a select element."""

    payload: list[dict[str, str]] = await page.locator(selector).evaluate(
        """
        (node) => {
          if (!(node instanceof HTMLSelectElement)) {
            return [];
          }
          return Array.from(node.options).map((opt) => ({
            value: opt.value || '',
            name: (opt.textContent || '').trim(),
          }));
        }
        """
    )
    return [
        CenterInfo(value=item.get("value", ""), name=item.get("name", ""))
        for item in payload
        if item.get("value") is not None
    ]


async def _extract_exam_cards(
    page: Page,
    enriched_details: dict[int, list[BatchDetailInfo]] | None = None,
) -> list[CourseCardInfo]:
    """Extract structured course-card details when present."""

    payload: list[dict[str, Any]] = await page.evaluate(
        """
        () => {
          const cards = Array.from(document.querySelectorAll('.exam-batches .course-card'));
          return cards.map((node) => {
            const optionTexts = Array.from(node.querySelectorAll('.cmbHomeExamBatch option'))
              .map((opt) => (opt.textContent || '').trim())
              .filter((text) => text && text !== '--Select--');
            const q = (selector) => (node.querySelector(selector)?.innerText || '').trim();
            return {
              title: (node.querySelector('h6')?.innerText || '').trim(),
              text: (node.innerText || '').trim(),
              batch_options: optionTexts,
              exam_dates_text: q('.exam-dates'),
              registration_dates_text: q('.exam-registration-dates'),
              external_fee_text: q('.exam-external-fee'),
              internal_fee_text: q('.exam-internal-fee'),
              external_seats_text: q('.seats-external'),
              internal_seats_text: q('.seats-internal'),
              has_external_register: !!node.querySelector('.btn-pay-external-exam'),
              has_internal_register: !!node.querySelector('.btn-pay-internal-exam'),
              is_blocked: !!node.querySelector('.seats-blocked-wrapper'),
            };
          });
        }
        """
    )
    return [
        CourseCardInfo(
            title=item.get("title", ""),
            text=item.get("text", ""),
            batch_options=list(item.get("batch_options", [])),
            exam_dates_text=item.get("exam_dates_text", ""),
            registration_dates_text=item.get("registration_dates_text", ""),
            external_fee_text=item.get("external_fee_text", ""),
            internal_fee_text=item.get("internal_fee_text", ""),
            external_seats_text=item.get("external_seats_text", ""),
            internal_seats_text=item.get("internal_seats_text", ""),
            has_external_register=bool(item.get("has_external_register", False)),
            has_internal_register=bool(item.get("has_internal_register", False)),
            is_blocked=bool(item.get("is_blocked", False)),
            batch_details=list((enriched_details or {}).get(index, [])),
        )
        for index, item in enumerate(payload)
    ]


def _text_contains_any(text: str, keywords: list[str]) -> bool:
    """Return true when the given text contains any keyword."""

    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


async def _extract_batch_detail_from_card(card_locator: Any) -> dict[str, str]:
    """Read the currently populated batch detail fields from a course card."""

    return await card_locator.evaluate(
        """
        (node) => {
          const q = (selector) => (node.querySelector(selector)?.innerText || '').trim();
          return {
            exam_dates_text: q('.exam-dates'),
            registration_dates_text: q('.exam-registration-dates'),
            external_fee_text: q('.exam-external-fee'),
            internal_fee_text: q('.exam-internal-fee'),
            external_seats_text: q('.seats-external'),
            internal_seats_text: q('.seats-internal'),
          };
        }
        """
    )


async def _capture_screenshot(page: Page, screenshot_path: Path) -> bool:
    """Capture a screenshot within a bounded timeout."""

    try:
        async with asyncio.timeout(SCREENSHOT_TIMEOUT_MS / 1000):
            await page.screenshot(path=str(screenshot_path), full_page=True)
        return True
    except Exception:
        return False


async def _capture_html(page: Page) -> str:
    """Capture page HTML without letting failures mask the original error."""

    try:
        async with asyncio.timeout(5):
            return await page.content()
    except Exception:
        return ""


async def _enrich_partner_exam_cards(
    page: Page,
    course_keywords: list[str],
    logger: Any,
    timeout_ms: int,
    max_batches_per_card: int,
) -> dict[int, list[BatchDetailInfo]]:
    """Probe batch-detail responses for matching partner-portal cards."""

    card_count = await page.locator(".exam-batches .course-card").count()
    enriched: dict[int, list[BatchDetailInfo]] = {}
    for card_index in range(card_count):
        card_locator = page.locator(".exam-batches .course-card").nth(card_index)
        title = (await card_locator.locator("h6").inner_text()) if await card_locator.locator("h6").count() else ""
        text = await card_locator.inner_text()
        if course_keywords and not _text_contains_any(f"{title}\n{text}", course_keywords):
            continue

        options: list[dict[str, str]] = await card_locator.locator(".cmbHomeExamBatch option").evaluate_all(
            """
            (nodes) => nodes
              .map((node) => ({
                value: node.value || '',
                label: (node.textContent || '').trim(),
              }))
              .filter((item) => item.value && item.label && item.label !== '--Select--')
            """
        )
        details: list[BatchDetailInfo] = []
        for option in options[:max_batches_per_card]:
            batch_id = option["value"]
            batch_label = option["label"]
            response_status: int | None = None
            response_error: str | None = None
            try:
                async with page.expect_response(
                    lambda response, expected=batch_id: f"/home-exam-list?cmbbatchId={expected}" in response.url,
                    timeout=timeout_ms,
                ) as response_info:
                    await card_locator.locator(".cmbHomeExamBatch").evaluate(
                        """
                        (node, selectedValue) => {
                          if (!(node instanceof HTMLSelectElement)) {
                            throw new Error('Batch selector did not resolve to a <select> element');
                          }
                          node.value = selectedValue;
                          node.dispatchEvent(new Event('input', { bubbles: true }));
                          node.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        """,
                        batch_id,
                    )
                response = await response_info.value
                response_status = response.status
                if response_status >= 400:
                    response_error = await response.text()
            except Exception as exc:
                response_error = str(exc)

            await asyncio.sleep(1.2)
            snapshot = await _extract_batch_detail_from_card(card_locator)
            details.append(
                BatchDetailInfo(
                    batch_id=batch_id,
                    label=batch_label,
                    response_status=response_status,
                    error=response_error,
                    exam_dates_text=snapshot.get("exam_dates_text", ""),
                    registration_dates_text=snapshot.get("registration_dates_text", ""),
                    external_fee_text=snapshot.get("external_fee_text", ""),
                    internal_fee_text=snapshot.get("internal_fee_text", ""),
                    external_seats_text=snapshot.get("external_seats_text", ""),
                    internal_seats_text=snapshot.get("internal_seats_text", ""),
                )
            )
            logger.info(
                "browser_batch_detail_probe",
                extra={
                    "card_index": card_index,
                    "title": title,
                    "batch_id": batch_id,
                    "response_status": response_status,
                    "has_exam_dates": bool(snapshot.get("exam_dates_text")),
                    "has_registration_dates": bool(snapshot.get("registration_dates_text")),
                    "has_seats": bool(snapshot.get("external_seats_text") or snapshot.get("internal_seats_text")),
                },
            )
        if details:
            enriched[card_index] = details
    return enriched


async def _wait_for_any_selector(page: Page, selectors: list[str], timeout_ms: int) -> str | None:
    """Wait until any selector appears and return the first match."""

    if not selectors:
        return None
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        for selector in selectors:
            if not selector:
                continue
            try:
                if await page.locator(selector).count() > 0:
                    return selector
            except Exception:
                continue
        await asyncio.sleep(0.2)
    return None


async def _apply_interaction_step(page: Page, step: InteractionStep, logger: Any, timeout_ms: int) -> None:
    """Execute one configured interaction step on the page."""

    locator = page.locator(step.selector)
    if step.action == "select":
        async def _select() -> None:
            await locator.first.evaluate(
                """
                (node, selectedValue) => {
                  if (!(node instanceof HTMLSelectElement)) {
                    throw new Error('Configured select interaction did not resolve to a <select> element');
                  }
                  node.value = selectedValue;
                  node.dispatchEvent(new Event('input', { bubbles: true }));
                  node.dispatchEvent(new Event('change', { bubbles: true }));
                }
                """,
                step.value or "",
            )

        if step.wait_for_request_contains:
            async with page.expect_response(
                lambda response: step.wait_for_request_contains in response.url,
                timeout=timeout_ms,
            ):
                await _select()
        else:
            await _select()
    elif step.action == "click":
        async def _click() -> None:
            await locator.first.click()

        if step.wait_for_request_contains:
            async with page.expect_response(
                lambda response: step.wait_for_request_contains in response.url,
                timeout=timeout_ms,
            ):
                await _click()
        else:
            await _click()
    else:
        raise ValueError(f"Unsupported interaction action: {step.action}")

    if step.post_delay_ms > 0:
        await asyncio.sleep(step.post_delay_ms / 1000)
    if step.wait_for_selectors:
        await _wait_for_any_selector(page, step.wait_for_selectors, timeout_ms)
    logger.info(
        "browser_interaction_applied",
        extra={
            "action": step.action,
            "selector": step.selector,
            "value": step.value,
            "wait_for_request_contains": step.wait_for_request_contains,
        },
    )


async def _build_context(browser: Browser) -> BrowserContext:
    """Create a randomized browser context."""

    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport=_random_viewport(),
        locale="de-DE",
        timezone_id="Asia/Kolkata",
        java_script_enabled=True,
        ignore_https_errors=False,
    )
    await _apply_stealth(context)
    return context


async def fetch_page(
    url: str,
    screenshot_dir: Path,
    selectors: list[str],
    logger: Any,
    interaction_steps: list[InteractionStep] | None = None,
    system: str | None = None,
    course_keywords: list[str] | None = None,
    detail_probe_max_batches: int = 0,
    wait_for_selectors: list[str] | None = None,
    wait_timeout_ms: int = 12_000,
    timeout_ms: int = 45_000,
    max_attempts: int = 3,
) -> PageResult:
    """Fetch a page with retries and return a normalized extraction result."""

    screenshot_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for attempt in range(1, max_attempts + 1):
                context = await _build_context(browser)
                page = await context.new_page()
                screenshot_path = screenshot_dir / (
                    f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{attempt}.png"
                )
                start = time.perf_counter()
                try:
                    await asyncio.sleep(random.uniform(0.8, 2.0))
                    await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                    matched_wait_selector = await _wait_for_any_selector(
                        page,
                        wait_for_selectors or [],
                        wait_timeout_ms,
                    )
                    for step in interaction_steps or []:
                        await _apply_interaction_step(page, step, logger, wait_timeout_ms)
                    await asyncio.sleep(random.uniform(1.0, 2.5))
                    await _humanize_page(page)
                    raw_html = await page.content()
                    visible_text = await page.locator("body").inner_text()
                    buttons = await _extract_buttons(page)
                    links = await _extract_links(page)
                    enriched_details: dict[int, list[BatchDetailInfo]] = {}
                    if system == "partner_portal" and detail_probe_max_batches > 0:
                        enriched_details = await _enrich_partner_exam_cards(
                            page,
                            course_keywords or [],
                            logger,
                            wait_timeout_ms,
                            detail_probe_max_batches,
                        )
                    exam_cards = await _extract_exam_cards(page, enriched_details)
                    selector_matches = await _extract_selector_matches(page, selectors)
                    if matched_wait_selector:
                        selector_matches[f"__wait__:{matched_wait_selector}"] = 1
                    page_title = await page.title()
                    await _capture_screenshot(page, screenshot_path)
                    load_time_ms = int((time.perf_counter() - start) * 1000)
                    return PageResult(
                        raw_html=raw_html,
                        visible_text=visible_text,
                        buttons=buttons,
                        links=links,
                        screenshot_path=str(screenshot_path),
                        page_title=page_title,
                        load_time_ms=load_time_ms,
                        error=None,
                        selector_matches=selector_matches,
                        exam_cards=exam_cards,
                    )
                except Exception as exc:
                    load_time_ms = int((time.perf_counter() - start) * 1000)
                    raw_html = await _capture_html(page)
                    await _capture_screenshot(page, screenshot_path)
                    logger.warning(
                        "browser_attempt_failed",
                        extra={
                            "attempt": attempt,
                            "url": url,
                            "error": str(exc),
                            "load_time_ms": load_time_ms,
                        },
                    )
                    if attempt >= max_attempts:
                        return PageResult(
                            raw_html=raw_html,
                            visible_text="",
                            buttons=[],
                            links=[],
                            screenshot_path=str(screenshot_path),
                            page_title="",
                            load_time_ms=load_time_ms,
                            error=str(exc),
                            selector_matches={selector: 0 for selector in selectors},
                            exam_cards=[],
                        )
                    backoff_seconds = min(15, 2 ** attempt)
                    logger.info(
                        "browser_retry_backoff",
                        extra={"attempt": attempt, "url": url, "backoff_seconds": backoff_seconds},
                    )
                    await asyncio.sleep(backoff_seconds)
                finally:
                    await page.close()
                    await context.close()
        finally:
            await browser.close()


async def discover_partner_centers(
    url: str,
    screenshot_dir: Path,
    select_selector: str,
    wait_for_selectors: list[str],
    logger: Any,
    timeout_ms: int = 45_000,
    max_attempts: int = 2,
) -> CenterDiscoveryResult:
    """Discover partner-portal centers from the live page."""

    screenshot_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for attempt in range(1, max_attempts + 1):
                context = await _build_context(browser)
                page = await context.new_page()
                screenshot_path = screenshot_dir / (
                    f"centers_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{attempt}.png"
                )
                start = time.perf_counter()
                try:
                    await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                    await _wait_for_any_selector(page, wait_for_selectors, 12_000)
                    await asyncio.sleep(1.5)
                    raw_html = await _capture_html(page)
                    visible_text = await page.locator("body").inner_text()
                    centers = await _extract_select_options(page, select_selector)
                    page_title = await page.title()
                    await _capture_screenshot(page, screenshot_path)
                    return CenterDiscoveryResult(
                        centers=centers,
                        raw_html=raw_html,
                        visible_text=visible_text,
                        screenshot_path=str(screenshot_path),
                        page_title=page_title,
                        load_time_ms=int((time.perf_counter() - start) * 1000),
                        error=None,
                    )
                except Exception as exc:
                    raw_html = await _capture_html(page)
                    await _capture_screenshot(page, screenshot_path)
                    if attempt >= max_attempts:
                        return CenterDiscoveryResult(
                            centers=[],
                            raw_html=raw_html,
                            visible_text="",
                            screenshot_path=str(screenshot_path),
                            page_title="",
                            load_time_ms=int((time.perf_counter() - start) * 1000),
                            error=str(exc),
                        )
                    logger.warning(
                        "center_discovery_attempt_failed",
                        extra={"attempt": attempt, "url": url, "error": str(exc)},
                    )
                    await asyncio.sleep(2 ** attempt)
                finally:
                    await page.close()
                    await context.close()
        finally:
            await browser.close()
