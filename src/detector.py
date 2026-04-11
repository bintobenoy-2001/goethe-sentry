"""Slot detection logic for Goethe booking pages."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from browser import BatchDetailInfo, CourseCardInfo, PageResult


DEFAULT_AVAILABLE_SELECTORS = [
    "button.book:not([disabled])",
    "a[href*='register']",
    "[class*='available']:not([disabled])",
]
DEFAULT_BOOKED_SELECTORS = [
    "button[disabled]",
    "[class*='fully-booked']",
    "[class*='ausgebucht']",
]
DATE_PATTERN = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")
SEAT_NUMBER_PATTERN = re.compile(r"remaining seats[^0-9]*(\d+)", re.IGNORECASE)
KNOWN_LISTING_CITIES = [
    "Mumbai",
    "Chennai",
    "Kolkata",
    "New Delhi",
    "Bangalore",
    "Pune",
    "Goa",
    "Noida",
    "Trichy",
]
LISTING_SIGNAL_TOKENS = ("DETAILS", "Paper-based", "Computer-based", "INR", "Select modules")


@dataclass(slots=True)
class DetectionResult:
    """Normalized detection output."""

    status: str
    confidence: str
    method: str
    matched: str
    signature: str = ""
    summary_lines: list[str] | None = None
    detail_recovered: bool = False
    page_signature: str | None = None
    seats_count: int | None = None


def _target_system(target: dict[str, Any]) -> str | None:
    """Return the normalized system type for a target."""

    return target.get("system_type") or target.get("system")


def _keyword_score(text: str, keywords: list[str]) -> tuple[int, list[str]]:
    """Score visible text against a keyword list."""

    lowered = text.lower()
    matched = [keyword for keyword in keywords if keyword.lower() in lowered]
    return len(matched), matched


def _course_keywords(target: dict[str, Any]) -> list[str]:
    """Resolve course keywords, falling back to course level in the label."""

    configured = [keyword for keyword in target.get("course_keywords", []) if keyword]
    if configured:
        return configured
    label = target.get("label", "").lower()
    for token in ("a1", "a2", "b1", "b2", "c1", "c2"):
        if token in label:
            return [token]
    return []


def _course_matches(card: CourseCardInfo, target: dict[str, Any]) -> bool:
    """Return true when a course card matches the target course and city."""

    haystack = f"{card.title}\n{card.text}".lower()
    course_keywords = _course_keywords(target)
    if course_keywords and not any(keyword.lower() in haystack for keyword in course_keywords):
        return False
    city_filter = (target.get("city_filter") or "").strip().lower()
    if city_filter and city_filter not in haystack:
        return False
    return True


def _extract_months(batch_options: list[str]) -> list[str]:
    """Extract unique month-year pairs from batch option labels."""

    months: list[str] = []
    for option in batch_options:
        match = re.search(
            r"(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{4})",
            option,
            re.IGNORECASE,
        )
        if match:
            month_label = f"{match.group(1).title()} {match.group(2)}"
            if month_label not in months:
                months.append(month_label)
    return months


def _card_seat_candidates(card: CourseCardInfo) -> list[int]:
    """Extract numeric seat counts from structured fields and fallback text."""

    candidates: list[int] = []
    for value in (
        card.external_seats_text,
        card.internal_seats_text,
        *[detail.external_seats_text for detail in card.batch_details],
        *[detail.internal_seats_text for detail in card.batch_details],
    ):
        if not value:
            continue
        for match in re.findall(r"\d+", value):
            candidates.append(int(match))
    for match in SEAT_NUMBER_PATTERN.findall(card.text):
        candidates.append(int(match))
    return candidates


def _build_partner_summary(cards: list[CourseCardInfo]) -> tuple[str, list[str], bool, int | None]:
    """Build a concise availability signature and message lines for partner cards."""

    titles = ", ".join(card.title for card in cards if card.title)
    batch_options = [option for card in cards for option in card.batch_options]
    months = _extract_months(batch_options)
    listed = batch_options[:3]
    extra_count = max(0, len(batch_options) - len(listed))
    all_details: list[BatchDetailInfo] = [detail for card in cards for detail in card.batch_details]
    successful_details = [
        detail
        for detail in all_details
        if detail.response_status and detail.response_status < 400 and (
            detail.exam_dates_text
            or detail.registration_dates_text
            or detail.external_seats_text
            or detail.internal_seats_text
        )
    ]
    seats_candidates = [seat for card in cards for seat in _card_seat_candidates(card)]
    seats_count = max(seats_candidates) if seats_candidates else None
    lines = [
        f"🎓 Course: {titles or 'Matched course'}",
        f"🗓 Months: {', '.join(months) if months else 'not parsed'}",
    ]
    if listed:
        suffix = f" (+{extra_count} more)" if extra_count else ""
        lines.append(f"📚 Batches: {' | '.join(listed)}{suffix}")
    if successful_details:
        first = successful_details[0]
        lines.append(f"📅 Exam date: {first.exam_dates_text or first.label}")
    elif listed:
        lines.append(f"📅 Exam date: {listed[0]}")
    else:
        lines.append("📅 Exam date: not exposed")
    if seats_count is not None:
        lines.append(f"💺 Seats remaining: {seats_count}")
    detail_signature = "||".join(
        f"{detail.batch_id}:{detail.response_status}:{detail.exam_dates_text}:{detail.registration_dates_text}:{detail.external_seats_text}:{detail.internal_seats_text}"
        for detail in all_details
    )
    signature = detail_signature or ("||".join(batch_options) if batch_options else titles)
    return signature, lines, bool(successful_details), seats_count


def _detect_partner_portal(target: dict[str, Any], page: PageResult, logger: logging.Logger) -> DetectionResult | None:
    """Detect partner-portal availability from visible exam cards."""

    if not page.exam_cards:
        return None
    relevant_cards = [card for card in page.exam_cards if _course_matches(card, target)]
    if not relevant_cards:
        logger.warning(
            "detection_unknown_no_matching_course",
            extra={"label": target.get("label", "unknown")},
        )
        return DetectionResult(
            status="unknown",
            confidence="low",
            method="unknown",
            matched="no matching course card found",
        )

    available_cards: list[CourseCardInfo] = []
    for card in relevant_cards:
        seat_candidates = _card_seat_candidates(card)
        has_positive_seats = any(seat > 0 for seat in seat_candidates)
        has_buttons = card.has_external_register or card.has_internal_register
        has_exam_dates = bool(card.batch_options) or bool(DATE_PATTERN.search(card.text))
        if has_buttons and has_exam_dates and (has_positive_seats or not seat_candidates) and not card.is_blocked:
            available_cards.append(card)

    if available_cards:
        signature, summary_lines, detail_recovered, seats_count = _build_partner_summary(available_cards)
        return DetectionResult(
            status="available",
            confidence="high",
            method="button",
            matched=", ".join(card.title for card in available_cards if card.title),
            signature=signature,
            summary_lines=summary_lines,
            detail_recovered=detail_recovered,
            seats_count=seats_count,
        )

    return DetectionResult(
        status="booked",
        confidence="medium",
        method="button",
        matched=", ".join(card.title for card in relevant_cards if card.title) or "matching course card found",
        signature="booked",
        summary_lines=[
            f"🎓 Course: {', '.join(card.title for card in relevant_cards if card.title)}",
            "📚 No bookable rows right now",
        ],
    )


def _compute_page_signature(page: PageResult) -> str:
    """Hash the signature source used for unified-page change tracking."""

    signature_source = page.visible_text or page.page_signature_source
    return hashlib.md5(signature_source.encode("utf-8", errors="ignore")).hexdigest()


def _find_city_listing_block(text: str, city_filter: str) -> tuple[str | None, str | None, str | None]:
    """Find a localized text window that looks like a live listing card for the requested city."""

    city_pattern = re.compile(re.escape(city_filter), re.IGNORECASE)
    for match in city_pattern.finditer(text):
        start = max(0, match.start() - 350)
        end = min(len(text), match.end() + 350)
        block = text[start:end].strip()
        if not DATE_PATTERN.search(block):
            continue
        if not any(token.lower() in block.lower() for token in (token.lower() for token in LISTING_SIGNAL_TOKENS)):
            continue
        exam_date = DATE_PATTERN.search(block)
        format_match = re.search(r"(Paper-based|Computer-based)", block, re.IGNORECASE)
        return block, exam_date.group(0) if exam_date else None, format_match.group(0) if format_match else None
    return None, None, None


def _detect_goethe_listing(target: dict[str, Any], page: PageResult) -> DetectionResult:
    """Detect city availability from the single official Goethe listing page."""

    city_filter = target.get("city_filter", "")
    page_signature = _compute_page_signature(page)
    block, exam_date, exam_format = _find_city_listing_block(page.visible_text, city_filter)
    if block:
        summary_lines = []
        if exam_date:
            summary_lines.append(f"📅 Date: {exam_date}")
        if exam_format:
            summary_lines.append(f"🏫 Format: {exam_format}")
        return DetectionResult(
            status="available",
            confidence="high",
            method="visible_text_block",
            matched=block[:200],
            signature=f"{city_filter}:{exam_date or block[:80]}",
            summary_lines=summary_lines,
            page_signature=page_signature,
        )

    lowered_html = page.raw_html.lower()
    city_html = city_filter.lower()
    city_idx = lowered_html.find(city_html)
    if city_idx != -1:
        window = lowered_html[max(0, city_idx - 500) : city_idx + 500]
        if "details" in window or "btn" in window or "booking" in window:
            return DetectionResult(
                status="available",
                confidence="medium",
                method="html_proximity",
                matched=f"{city_filter} near listing markup",
                signature=f"{city_filter}:html",
                page_signature=page_signature,
            )

    other_cities_visible = [city for city in KNOWN_LISTING_CITIES if city.lower() != city_filter.lower() and _find_city_listing_block(page.visible_text, city)[0]]
    if other_cities_visible:
        return DetectionResult(
            status="booked",
            confidence="high",
            method="listing_absent",
            matched=f"{city_filter} not present while other cities are listed",
            signature=f"{city_filter}:absent",
            page_signature=page_signature,
        )

    if "dates cannot be displayed temporarily" in page.visible_text:
        return DetectionResult(
            status="booked",
            confidence="high",
            method="listing_absent_error",
            matched="dates cannot be displayed temporarily (likely booked/closed)",
            signature=f"{city_filter}:error_message",
            page_signature=page_signature,
        )

    return DetectionResult(
        status="unknown",
        confidence="low",
        method="unknown",
        matched="no confident signal",
        page_signature=page_signature,
    )


def detect_slot(page: PageResult, target: dict[str, Any], logger: logging.Logger) -> DetectionResult:
    """Classify a page as available, booked, or unknown."""

    if page.error:
        logger.warning(
            "detection_unknown_page_error",
            extra={"label": target.get("label", "unknown"), "error": page.error},
        )
        return DetectionResult(
            status="unknown",
            confidence="low",
            method="unknown",
            matched=page.error,
        )

    system = _target_system(target)
    if system == "goethe_listing":
        return _detect_goethe_listing(target, page)

    if system == "partner_portal":
        partner_result = _detect_partner_portal(target, page, logger)
        if partner_result is not None:
            return partner_result

    available_selectors = target.get("available_selectors") or DEFAULT_AVAILABLE_SELECTORS
    booked_selectors = target.get("booked_selectors") or DEFAULT_BOOKED_SELECTORS
    matched_available = [selector for selector in available_selectors if page.selector_matches.get(selector, 0) > 0]
    if matched_available:
        return DetectionResult(
            status="available",
            confidence="high",
            method="selector",
            matched=", ".join(matched_available),
            signature="|".join(matched_available),
        )
    matched_booked = [selector for selector in booked_selectors if page.selector_matches.get(selector, 0) > 0]
    if matched_booked:
        return DetectionResult(
            status="booked",
            confidence="high",
            method="selector",
            matched=", ".join(matched_booked),
            signature="|".join(matched_booked),
        )
    return DetectionResult(
        status="unknown",
        confidence="low",
        method="unknown",
        matched="no confident signal",
    )
