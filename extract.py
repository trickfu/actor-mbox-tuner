#!/usr/bin/env python3
"""Regex-based order extraction logic mirroring the Apps Script Extractor.gs flow."""

from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parseaddr
from typing import Any


NOT_FOUND = "NOT FOUND"


SENDER_RULES: dict[str, dict[str, Any]] = {
    "amazon.com": {
        "store": "Amazon",
        "order_number_patterns": [
            r"\border\s*#\s*([0-9]{3}-[0-9]{7}-[0-9]{7})\b",
            r"\border\s+number\s*[:#]?\s*([0-9]{3}-[0-9]{7}-[0-9]{7})\b",
        ],
        "total_patterns": [
            r"\border\s+total\s*[:\-]?\s*([$€£])\s*([0-9,]+(?:\.[0-9]{2})?)",
            r"\btotal\s*[:\-]?\s*([$€£])\s*([0-9,]+(?:\.[0-9]{2})?)",
        ],
    },
    "etsy.com": {
        "store": "Etsy",
        "order_number_patterns": [
            r"\border\s*#\s*([0-9]{6,})\b",
            r"\breceipt\s*#\s*([0-9]{6,})\b",
        ],
        "total_patterns": [
            r"\border\s+total\s*[:\-]?\s*([$€£])\s*([0-9,]+(?:\.[0-9]{2})?)",
            r"\btotal\s*[:\-]?\s*([$€£])\s*([0-9,]+(?:\.[0-9]{2})?)",
        ],
    },
    # Add high-volume sender domains from analyze.py here, for example:
    # "example.com": {
    #     "store": "Example Store",
    #     "order_number_patterns": [r"..."],
    #     "total_patterns": [r"..."],
    # },
}


GENERIC_ORDER_NUMBER_PATTERNS = [
    r"\border\s*(?:number|no\.?|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{4,})\b",
    r"\bconfirmation\s*(?:number|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{4,})\b",
    r"\breceipt\s*(?:number|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{4,})\b",
]

GENERIC_TOTAL_PATTERNS = [
    r"\border\s+total\s*[:\-]?\s*([$€£])\s*([0-9,]+(?:\.[0-9]{2})?)",
    r"\bgrand\s+total\s*[:\-]?\s*([$€£])\s*([0-9,]+(?:\.[0-9]{2})?)",
    r"\btotal\s*[:\-]?\s*([$€£])\s*([0-9,]+(?:\.[0-9]{2})?)",
    r"\bamount\s+paid\s*[:\-]?\s*([$€£])\s*([0-9,]+(?:\.[0-9]{2})?)",
]

ITEM_ANCHOR_PATTERNS = [
    r"\bitem\s*[:\-]\s*(.+?)(?=\s+(?:order\s+total|qty|quantity|price|total)\b|$)",
    r"\bproduct\s*[:\-]\s*(.+?)(?=\s+(?:order\s+total|qty|quantity|price|total)\b|$)",
    r"\byou\s+(?:bought|purchased|ordered)\s*[:\-]?\s*(.+?)(?=\s+(?:order\s+total|qty|quantity|price|total)\b|$)",
    r"\bitem\s+ordered\s*[:\-]\s*(.+?)(?=\s+(?:order\s+total|qty|quantity|price|total)\b|$)",
]

ARRIVAL_DATE_PATTERNS = [
    r"\barriving\s+([A-Z][a-z]+\.?\s+\d{1,2}(?:,\s*\d{4})?)",
    r"\bestimated\s+(?:delivery|arrival)\s*[:\-]?\s*([A-Z][a-z]+\.?\s+\d{1,2}(?:,\s*\d{4})?)",
    r"\bdelivery\s+(?:date|by)\s*[:\-]?\s*([A-Z][a-z]+\.?\s+\d{1,2}(?:,\s*\d{4})?)",
    r"\b(?:will|should)\s+arrive\s+(?:by\s+)?([A-Z][a-z]+\.?\s+\d{1,2}(?:,\s*\d{4})?)",
    r"\barrives\s+([A-Z][a-z]+\.?\s+\d{1,2}(?:,\s*\d{4})?)",
]

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
UI_BUTTON_PHRASES = {
    "track package",
    "view order",
    "view details",
    "manage order",
    "leave feedback",
    "write a review",
    "track shipment",
}
MARKETING_FILLER_WORDS = {
    "premium",
    "new",
    "upgraded",
    "2024",
    "2025",
    "professional",
    "heavy",
    "duty",
}
SKIP_ITEM_LINES_RE = re.compile(
    r"(view|manage|order|total|subtotal|shipping|tax|payment|receipt|confirmation|tracking|delivered|arriving)",
    re.I,
)


def normalize_body(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def get_domain(sender_email: str | None) -> str:
    _, parsed_email = parseaddr(sender_email or "")
    address = (parsed_email or sender_email or "").strip().lower()
    if "@" in address:
        address = address.rsplit("@", 1)[1]
    labels = [label for label in address.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)

    full_domain = ".".join(labels)
    for known_domain in SENDER_RULES:
        if full_domain == known_domain or full_domain.endswith("." + known_domain):
            return known_domain
    return ".".join(labels[-2:])


def get_store_name(sender_email: str | None, sender_name: str | None) -> str:
    domain = get_domain(sender_email)
    if domain in SENDER_RULES:
        return SENDER_RULES[domain]["store"]
    if sender_name:
        return sender_name.strip()
    return domain or NOT_FOUND


def _first_match(patterns: list[str], body: str) -> re.Match[str] | None:
    for pattern in patterns:
        match = re.search(pattern, body, re.I)
        if match:
            return match
    return None


def extract_order_number(body: str, domain: str) -> str:
    normalized = normalize_body(body)
    domain_patterns = SENDER_RULES.get(domain, {}).get("order_number_patterns", [])
    match = _first_match(domain_patterns, normalized) or _first_match(
        GENERIC_ORDER_NUMBER_PATTERNS,
        normalized,
    )
    return match.group(1).strip() if match else NOT_FOUND


def extract_total(body: str, domain: str) -> dict[str, str]:
    normalized = normalize_body(body)
    domain_patterns = SENDER_RULES.get(domain, {}).get("total_patterns", [])
    match = _first_match(domain_patterns, normalized) or _first_match(GENERIC_TOTAL_PATTERNS, normalized)
    if not match:
        return {"total": NOT_FOUND, "currency": NOT_FOUND}

    symbol = match.group(1)
    amount = match.group(2).replace(",", "")
    return {"total": amount, "currency": CURRENCY_SYMBOLS.get(symbol, symbol)}


def clean_item_candidate(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" -:|")
    value = re.sub(r"\b(qty|quantity)\s*[:#]?\s*\d+.*$", "", value, flags=re.I).strip()
    return value


def is_ui_button_phrase(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value or "").strip().lower()
    normalized = normalized.strip(" .,!?:;-")
    return normalized in UI_BUTTON_PHRASES


def _looks_like_item_line(line: str) -> bool:
    line = clean_item_candidate(line)
    if not line or len(line) < 3 or len(line) > 140:
        return False
    if is_ui_button_phrase(line):
        return False
    if SKIP_ITEM_LINES_RE.search(line):
        return False
    if re.search(r"^[$€£]?\d+(?:\.\d{2})?$", line):
        return False
    return bool(re.search(r"[A-Za-z]", line))


def _clean_subject_item_name(raw_name: str) -> tuple[str, bool]:
    name = clean_item_candidate(raw_name)
    truncated = bool(re.search(r"\.\.\.$", name))
    name = re.sub(r"\.\.\.$", "", name).strip()
    return name, truncated


def _subject_item_result(raw_name: str, subject: str) -> dict[str, Any] | None:
    name, truncated = _clean_subject_item_name(raw_name)
    if not name or is_ui_button_phrase(name):
        return None
    return {
        "itemName": name,
        "items": [name],
        "multipleItems": bool(re.search(r"\band\s+\d+\s+more\s+items?\b", subject, re.I)),
        "itemNameTruncated": truncated,
    }


def extractItemNameFromSubject(subject: str | None) -> dict[str, Any] | None:
    subject = normalize_body(subject)
    if not subject:
        return None

    patterns = [
        r"\b(?:ordered|shipped|delivered|out\s+for\s+delivery|delivery\s+update|delivery\s+estimate\s+update):\s+\"([^\"]+)\"",
        r"\b(?:your\s+amazon\.com\s+order\s+of|shipped:)\s+\"\d+\"\s*x\s+(.+?)(?=\s+(?:has\s+shipped|shipped|and\s+\d+\s+more\s+items?)|[!.]?$)",
        r"\b(?:your\s+amazon\.com\s+order\s+of|shipped:)\s+(?:\d+\s*x\s*)?\"([^\"]+)\"",
        r"\b(?:your\s+amazon\.com\s+order\s+of|shipped:)\s+\d+\s+\"([^\"]+)\"",
    ]
    for pattern in patterns:
        match = re.search(pattern, subject, re.I)
        if match:
            return _subject_item_result(match.group(1), subject)
    return None


def extractAmazonItemsFromBody(body: str | None) -> list[str]:
    normalized = normalize_body(body)
    matches = re.findall(r"(?:^|\s)\*\s+(.+?)\s+(?:Quantity|Qty)\s*:\s*\d+\b", normalized, re.I)
    return [
        candidate
        for candidate in (clean_item_candidate(match) for match in matches)
        if candidate and not is_ui_button_phrase(candidate)
    ]


def extract_item_name(body: str, domain: str) -> str:
    del domain
    normalized = normalize_body(body)
    for pattern in ITEM_ANCHOR_PATTERNS:
        match = re.search(pattern, normalized, re.I)
        if match:
            candidate = clean_item_candidate(match.group(1))
            if candidate and not is_ui_button_phrase(candidate):
                return candidate

    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if re.search(r"\b(qty|quantity)\s*[:#]?\s*\d+\b", line, re.I):
            for previous in reversed(lines[max(0, index - 4) : index]):
                if _looks_like_item_line(previous):
                    return clean_item_candidate(previous)

    return NOT_FOUND


def normalizeItemName(rawName: str | None) -> dict[str, Any]:
    value = (rawName or "").lower()
    value = re.sub(r"\b\d+\s*pcs\b", " ", value)
    value = re.sub(r"\b\d+\s*pack\b", " ", value)
    value = re.sub(r"\b\d+\s*-\s*pack\b", " ", value)
    value = re.sub(r"\bx\d+\b", " ", value)
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    words = [
        word
        for word in re.sub(r"\s+", " ", value).strip().split()
        if word and word not in MARKETING_FILLER_WORDS
    ]
    key_words = words[:6]
    return {"cleaned": " ".join(key_words), "words": key_words}


def extract_arrival_date(body: str) -> date | None:
    normalized = normalize_body(body)
    for pattern in ARRIVAL_DATE_PATTERNS:
        match = re.search(pattern, normalized, re.I)
        if not match:
            continue
        parsed = parse_date_candidate(match.group(1))
        if parsed:
            return parsed
    return None


def parse_date_candidate(value: str) -> date | None:
    value = value.strip().replace(".", "")
    formats = ["%B %d, %Y", "%b %d, %Y", "%B %d", "%b %d"]
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=date.today().year)
            return parsed.date()
        except ValueError:
            continue
    return None


def extract_order_data(email_obj: dict[str, Any]) -> dict[str, Any]:
    sender_email = email_obj.get("sender_email", "")
    sender_name = email_obj.get("sender_name", "")
    body = email_obj.get("body_text", "")
    domain = get_domain(sender_email)

    order_number = extract_order_number(body, domain)
    total_data = extract_total(body, domain)
    amazon_body_items = extractAmazonItemsFromBody(body) if domain == "amazon.com" else []
    subject_item = (
        extractItemNameFromSubject(email_obj.get("subject", ""))
        if domain == "amazon.com" and not amazon_body_items
        else None
    )
    if amazon_body_items:
        item_name = amazon_body_items[0]
        items = amazon_body_items
        item_name_truncated = False
        multiple_items = len(amazon_body_items) > 1
        item_name_source = "amazon_body"
    elif subject_item:
        item_name = subject_item["itemName"]
        items = subject_item["items"]
        item_name_truncated = subject_item["itemNameTruncated"]
        multiple_items = subject_item["multipleItems"]
        item_name_source = "subject"
    else:
        item_name = extract_item_name(body, domain)
        items = [] if item_name == NOT_FOUND else [item_name]
        item_name_truncated = False
        multiple_items = bool(re.search(r"\b(\d+)\s+(items|products)\b", normalize_body(body), re.I))
        item_name_source = "not_found" if item_name == NOT_FOUND else "body"
    normalized_item = normalizeItemName(item_name if item_name != NOT_FOUND else "")
    arrival_date = extract_arrival_date(body)

    hits = [
        order_number != NOT_FOUND,
        total_data["total"] != NOT_FOUND,
        item_name != NOT_FOUND,
        arrival_date is not None,
    ]
    confidence = round(sum(hits) / len(hits), 2)

    return {
        "orderNumber": order_number,
        "total": total_data["total"],
        "currency": total_data["currency"],
        "store": get_store_name(sender_email, sender_name),
        "itemName": item_name,
        "items": items,
        "itemNameSource": item_name_source,
        "itemNameTruncated": item_name_truncated,
        "itemNameNormalized": normalized_item["cleaned"],
        "itemNameWords": normalized_item["words"],
        "multipleItems": multiple_items,
        "arrivalDate": arrival_date.isoformat() if arrival_date else NOT_FOUND,
        "confidence": confidence,
        "needsReview": confidence < 0.75,
    }
