"""Slot detection logic for Goethe booking pages."""

from __future__ import annotations

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


def _keyword_score(text: str, keywords: list[str]) -> tuple[int, list[str]]:
    """Score visible text against a keyword list."""

    lowered = text.lower()
    matched = [keyword for keyword in keywords if keyword.lower() in lowered]
    return len(matched), matched


def _unknown_keyword_match(text: str, keywords: list[str]) -> list[str]:
    """Return unknown-state keywords found in visible text."""

    lowered = text.lower()
    return [keyword for keyword in keywords if keyword.lower() in lowered]


def _course_matches(card: CourseCardInfo, course_keywords: list[str]) -> bool:
    """Return true when a course card matches the configured target keywords."""

    if not course_keywords:
        return True
    haystack = f"{card.title}\n{card.text}".lower()
    return any(keyword.lower() in haystack for keyword in course_keywords)


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


def _build_partner_summary(cards: list[CourseCardInfo]) -> tuple[str, list[str], bool]:
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
    failed_details = [
        detail
        for detail in all_details
        if detail.response_status is None or detail.response_status >= 400
    ]
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
        lines.append(
            f"📝 Registration: {first.registration_dates_text or 'not exposed for successful detail response'}"
        )
        lines.append(
            f"💺 Seats: {first.external_seats_text or first.internal_seats_text or 'not exposed for successful detail response'}"
        )
    elif failed_details:
        statuses = ", ".join(
            str(detail.response_status) if detail.response_status is not None else "timeout"
            for detail in failed_details[:3]
        )
        lines.append("📅 Exam date: using list-view batch labels only")
        lines.append(f"📝 Registration: detail probe failed ({statuses})")
        lines.append("💺 Seats: detail probe failed")
    else:
        lines.append("📅 Exam date: not exposed in list view")
        lines.append("📝 Registration: not exposed")
        lines.append("💺 Seats: not exposed")
    detail_signature = "||".join(
        f"{detail.batch_id}:{detail.response_status}:{detail.exam_dates_text}:{detail.registration_dates_text}:{detail.external_seats_text}:{detail.internal_seats_text}"
        for detail in all_details
    )
    signature = detail_signature or ("||".join(batch_options) if batch_options else titles)
    return signature, lines, bool(successful_details)


def _detect_partner_portal(target: dict[str, Any], page: PageResult, logger: logging.Logger) -> DetectionResult | None:
    """Specialized detection for shared partner-portal exam cards."""

    if not page.exam_cards:
        return None
    course_keywords = target.get("course_keywords", [])
    relevant_cards = [card for card in page.exam_cards if _course_matches(card, course_keywords)]
    if not relevant_cards:
        logger.warning(
            "detection_unknown_no_matching_course",
            extra={"label": target.get("label", "unknown"), "course_keywords": course_keywords},
        )
        return DetectionResult(
            status="unknown",
            confidence="low",
            method="unknown",
            matched="no matching course card found",
        )

    available_cards = [
        card
        for card in relevant_cards
        if card.batch_options and (card.has_external_register or card.has_internal_register) and not card.is_blocked
    ]
    if available_cards:
        signature, summary_lines, detail_recovered = _build_partner_summary(available_cards)
        return DetectionResult(
            status="available",
            confidence="high",
            method="button",
            matched=", ".join(card.title for card in available_cards if card.title),
            signature=signature,
            summary_lines=summary_lines,
            detail_recovered=detail_recovered,
        )

    return DetectionResult(
        status="booked",
        confidence="medium",
        method="button",
        matched=", ".join(card.title for card in relevant_cards if card.title) or "matching course card found",
        signature="booked",
        summary_lines=[f"🎓 Course: {', '.join(card.title for card in relevant_cards if card.title)}", "📚 No listed batches right now"],
    )


def detect_slot(page: PageResult, target: dict[str, Any], logger: logging.Logger) -> DetectionResult:
    """Classify a page as available, booked, or unknown."""

    if target.get("system") == "partner_portal":
        partner_result = _detect_partner_portal(target, page, logger)
        if partner_result is not None:
            return partner_result

    available_selectors = target.get("available_selectors") or DEFAULT_AVAILABLE_SELECTORS
    booked_selectors = target.get("booked_selectors") or DEFAULT_BOOKED_SELECTORS
    unknown_keywords = target.get("unknown_keywords", [])

    unknown_hits = _unknown_keyword_match(page.visible_text, unknown_keywords)
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
    if unknown_hits:
        logger.warning(
            "detection_unknown_keyword",
            extra={"label": target.get("label", "unknown"), "matched": unknown_hits},
        )
        return DetectionResult(
            status="unknown",
            confidence="medium",
            method="unknown",
            matched=", ".join(unknown_hits),
        )

    matched_available = [selector for selector in available_selectors if page.selector_matches.get(selector, 0) > 0]
    if matched_available:
        return DetectionResult(
            status="available",
            confidence="high",
            method="button",
            matched=", ".join(matched_available),
            signature="|".join(matched_available),
        )

    matched_booked = [selector for selector in booked_selectors if page.selector_matches.get(selector, 0) > 0]
    if matched_booked:
        return DetectionResult(
            status="booked",
            confidence="high",
            method="button",
            matched=", ".join(matched_booked),
            signature="|".join(matched_booked),
        )

    available_score, available_hits = _keyword_score(
        page.visible_text, target.get("available_keywords", [])
    )
    booked_score, booked_hits = _keyword_score(page.visible_text, target.get("booked_keywords", []))
    if available_score > booked_score and available_score >= 2:
        return DetectionResult(
            status="available",
            confidence="medium",
            method="keyword",
            matched=", ".join(available_hits),
            signature="|".join(available_hits),
        )
    if booked_score > available_score and booked_score >= 1:
        return DetectionResult(
            status="booked",
            confidence="medium",
            method="keyword",
            matched=", ".join(booked_hits),
            signature="|".join(booked_hits),
        )

    logger.warning(
        "detection_unknown",
        extra={"label": target.get("label", "unknown"), "title": page.page_title},
    )
    return DetectionResult(
        status="unknown",
        confidence="low",
        method="unknown",
        matched="no confident signal",
    )
