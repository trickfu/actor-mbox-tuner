#!/usr/bin/env python3
"""Analyze extraction coverage by sender domain and write CSV results."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import extract


CSV_FIELDS = [
    "domain",
    "status",
    "message_ids",
    "subject",
    "order_number",
    "total",
    "item_name",
    "items",
    "item_name_source",
    "item_name_normalized",
    "item_name_truncated",
    "arrival_date",
    "arrival_date_source",
    "confidence",
    "needs_review",
]
AMAZON_LINE_ITEM_CSV_FIELDS = [
    "order_number",
    "item_name_normalized",
    "quantity",
    "current_status",
    "ordered_date",
    "shipped_date",
    "delivered_date",
    "added_to_inventory",
    "num_contributing_emails",
]
AMAZON_LINE_ITEM_STATE_PATH = "amazon_line_items.json"
AMAZON_LINE_ITEM_CSV_PATH = "amazon_line_items.csv"
VERIFICATION_ORDER_NUMBER = "114-2441697-7949852"

FOOD_SENDERS = {
    "doordash.com",
    "messages.doordash.com",
    "ubereats.com",
    "grubhub.com",
    "seamless.com",
}
FOOD_SENDER_NAME_RE = re.compile(r"\b(doordash|uber\s*eats|grubhub)\b", re.I)
DOORDASH_SUBJECT_RE = re.compile(r"\border confirmation for .+ from .+", re.I)
IGNORE_SUBJECT_RE = re.compile(
    r"\b(deals?|login|inquiry|see handpicked|celebrate|dashpass|backup payment)\b",
    re.I,
)
ORDER_NUMBER_HINT_RE = re.compile(
    r"\b(?:order|confirmation|receipt|invoice|salesorder)\s*(?:number|no\.?|#)\s*[:#]?\s*[A-Z0-9][A-Z0-9\-]{4,}\b"
    r"|\border\s+[A-Z0-9\-]*\d[A-Z0-9\-]{4,}\b",
    re.I,
)
SHIPPING_STATUS_RE = re.compile(
    r"\b(ordered|shipped|shipping|delivered|out for delivery|delivery update|delivery estimate update|arriv(?:e|es|ing)|on the way|tracking)\b",
    re.I,
)
STATUS_RANK = {"Unknown": 0, "Ordered": 1, "Shipped": 2, "Out for delivery": 3, "Delivered": 4}
LAST_CATEGORY_COUNTS: Counter[str] = Counter()
LAST_RAW_INVENTORY_COUNT = 0


def is_hit(value: Any) -> bool:
    return value not in (None, "", extract.NOT_FOUND)


def format_rate(hits: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{hits / total:.0%}"


def sender_host(sender_email: str | None) -> str:
    address = (sender_email or "").strip().lower()
    if "@" in address:
        return address.rsplit("@", 1)[1]
    return address


def classifyEmailCategory(
    senderEmail: str | None,
    senderName: str | None,
    subject: str | None,
    bodyText: str | None,
) -> str:
    host = sender_host(senderEmail)
    domain = extract.get_domain(senderEmail)
    sender_name = extract.strip_invisible_chars(senderName)
    subject_text = extract.strip_invisible_chars(subject)
    body_text = extract.strip_invisible_chars(bodyText)
    combined = f"{subject_text} {body_text}"

    if host in FOOD_SENDERS or domain in FOOD_SENDERS or FOOD_SENDER_NAME_RE.search(sender_name):
        return "food"
    if DOORDASH_SUBJECT_RE.search(subject_text):
        return "food"
    if IGNORE_SUBJECT_RE.search(subject_text):
        return "ignore"
    if not ORDER_NUMBER_HINT_RE.search(combined) and not SHIPPING_STATUS_RE.search(combined):
        return "ignore"
    return "inventory"


def infer_status(subject: str | None, body_text: str | None) -> str:
    return extract.infer_email_status(subject, body_text)


def amazon_line_item_status(subject: str | None) -> str | None:
    subject_text = extract.normalize_body(subject)
    if re.match(r"^delivered\b", subject_text, re.I):
        return "Delivered"
    if re.match(r"^out\s+for\s+delivery\b", subject_text, re.I):
        return "Out for delivery"
    if re.match(r"^shipped\b", subject_text, re.I):
        return "Shipped"
    if re.search(r"\byour amazon\.com order of .+ has shipped\b", subject_text, re.I):
        return "Shipped"
    if re.match(r"^ordered\b", subject_text, re.I):
        return "Ordered"
    if re.search(r"\byour amazon\.com order of\b", subject_text, re.I):
        return "Ordered"
    return None


def amazon_line_item_flags(subject: str | None) -> dict[str, bool]:
    subject_text = extract.normalize_body(subject)
    return {
        "payment_declined": bool(re.search(r"\bpayment declined\b", subject_text, re.I)),
        "shipping_delayed": bool(re.search(r"\bdelay in shipping\b", subject_text, re.I)),
    }


def line_item_key(order_number: str, normalized_item_name: str) -> str:
    return f"{order_number}::{normalized_item_name}"


TOKEN_SYNONYMS = {"cable": "cord", "wire": "cord"}


def canonical_match_tokens(normalized_item_name: str) -> list[str]:
    tokens = []
    for token in normalized_item_name.split():
        canonical = TOKEN_SYNONYMS.get(token, token)
        if re.fullmatch(r"\d+/\d+\"?", canonical):
            canonical = canonical.rstrip('"')
        tokens.append(canonical)
    return tokens


def jaccard_for_tokens(left_tokens: set[str], right_tokens: set[str]) -> float:
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def should_merge_same_order_items(existing_normalized: str, new_normalized: str) -> bool:
    existing_tokens = canonical_match_tokens(existing_normalized)
    new_tokens = canonical_match_tokens(new_normalized)
    if len(existing_tokens) < 2 or len(new_tokens) < 2:
        return False

    existing_set = set(existing_tokens)
    new_set = set(new_tokens)
    if existing_set <= new_set or new_set <= existing_set:
        return True
    if jaccard_for_tokens(existing_set, new_set) >= 0.8:
        return True

    prefix_len = min(4, len(existing_tokens), len(new_tokens))
    if prefix_len >= 4 and existing_tokens[:prefix_len] == new_tokens[:prefix_len]:
        return True
    return False


def matching_line_item_key(
    state: dict[str, dict[str, Any]],
    order_number: str,
    normalized_item_name: str,
) -> str:
    if len(normalized_item_name.split()) < 2:
        return line_item_key(order_number, normalized_item_name)

    for existing_key, record in list(state.items()):
        if record.get("order_number") != order_number:
            continue
        existing_normalized = record.get("item_name_normalized", "")
        if not should_merge_same_order_items(existing_normalized, normalized_item_name):
            continue

        if len(normalized_item_name.split()) > len(existing_normalized.split()):
            new_key = line_item_key(order_number, normalized_item_name)
            state[new_key] = state.pop(existing_key)
            state[new_key]["item_name_normalized"] = normalized_item_name
            return new_key
        return existing_key

    return line_item_key(order_number, normalized_item_name)


def default_line_item_record(
    order_number: str,
    item_name_raw: str,
    item_name_normalized: str,
    quantity: int,
) -> dict[str, Any]:
    return {
        "order_number": order_number,
        "item_name_raw": item_name_raw,
        "item_name_normalized": item_name_normalized,
        "quantity": quantity,
        "current_status": "Unknown",
        "ordered_date": "",
        "shipped_date": "",
        "delivered_date": "",
        "last_seen_email_date": "",
        "contributing_message_ids": [],
        "added_to_inventory": False,
        "payment_declined": False,
        "shipping_delayed": False,
    }


def advance_line_item_status(record: dict[str, Any], status: str | None, email_date: str) -> None:
    if not status:
        return
    current_rank = STATUS_RANK.get(record.get("current_status", "Unknown"), 0)
    next_rank = STATUS_RANK[status]
    if next_rank > current_rank:
        record["current_status"] = status

    if status == "Ordered" and not record.get("ordered_date"):
        record["ordered_date"] = email_date
    elif status == "Shipped" and not record.get("shipped_date"):
        record["shipped_date"] = email_date
    elif status == "Out for delivery" and not record.get("shipped_date"):
        record["shipped_date"] = email_date
    elif status == "Delivered" and not record.get("delivered_date"):
        record["delivered_date"] = email_date

    if record["current_status"] == "Delivered" and not record.get("added_to_inventory"):
        record["added_to_inventory"] = True


def max_iso_date(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    return max(left, right)


def amazon_order_markers(normalized_body: str) -> list[re.Match[str]]:
    markers = list(re.finditer(r"\borderID=([0-9]{3}-[0-9]{7}-[0-9]{7})\b", normalized_body, re.I))
    if not markers:
        markers = list(re.finditer(r"\border\s*#\s*([0-9]{3}-[0-9]{7}-[0-9]{7})\b", normalized_body, re.I))
    return markers


def amazon_item_blocks_by_order(body: str | None, fallback_order_number: str) -> list[dict[str, Any]]:
    normalized_body = extract.normalize_body(body)
    order_matches = amazon_order_markers(normalized_body)
    item_blocks = extract.extractAmazonItemBlocksWithPositionsFromBody(normalized_body)
    if not order_matches:
        return [{**block, "order_number": fallback_order_number} for block in item_blocks]

    assigned_blocks = []
    for block in item_blocks:
        preceding_orders = [match for match in order_matches if match.start() <= block["position"]]
        order_number = preceding_orders[-1].group(1) if preceding_orders else fallback_order_number
        assigned_blocks.append({**block, "order_number": order_number})
    return assigned_blocks


def cross_order_attribution_audit(emails: list[dict[str, Any]]) -> dict[str, dict[str, bool]]:
    """Recompute nearest-preceding-orderID attribution per item block.

    Returns normalized_item_name -> {order_number: backed_by_real_marker}. An
    attribution is "real" when the block has a preceding orderID= URL marker;
    otherwise it is a fallback ("phantom") attribution.
    """
    attribution: dict[str, dict[str, bool]] = defaultdict(dict)
    for email_obj in emails:
        if extract.get_domain(email_obj.get("sender_email", "")) != "amazon.com":
            continue
        normalized_body = extract.normalize_body(email_obj.get("body_text", ""))
        fallback_order = extract.extract_order_number(email_obj.get("body_text", ""), "amazon.com")
        if not is_hit(fallback_order):
            fallback_order = extract.extract_order_number(
                f"{email_obj.get('subject', '')} {email_obj.get('body_text', '')}",
                "amazon.com",
            )
        markers = amazon_order_markers(normalized_body)
        for block in extract.extractAmazonItemBlocksWithPositionsFromBody(normalized_body):
            normalized = extract.normalizeItemName(block["item_name"])["cleaned"]
            if not normalized:
                continue
            preceding = [match for match in markers if match.start() <= block["position"]]
            if preceding:
                order_number, is_real = preceding[-1].group(1), True
            elif is_hit(fallback_order):
                order_number, is_real = fallback_order, False
            else:
                continue
            attribution[normalized][order_number] = attribution[normalized].get(order_number, False) or is_real
    return attribution


def print_cross_order_attribution_audit(emails: list[dict[str, Any]]) -> int:
    attribution = cross_order_attribution_audit(emails)
    multi_order = {name: orders for name, orders in attribution.items() if len(orders) >= 2}
    phantom_total = 0
    print("Cross-order attribution audit (items under 2+ orders):")
    if not multi_order:
        print("No item is attributed to more than one order.")
        return 0
    for name in sorted(multi_order):
        print(f"\n{name}")
        for order_number in sorted(multi_order[name]):
            backed = multi_order[name][order_number]
            phantom_total += 0 if backed else 1
            print(f"    {order_number}: {'true' if backed else 'PHANTOM'}")
    print(f"\nTotal phantom attributions: {phantom_total}")
    return phantom_total


def update_amazon_line_item_state(
    emails: list[dict[str, Any]],
    existing_state: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    state = {key: dict(value) for key, value in (existing_state or {}).items()}
    for email_obj in emails:
        if extract.get_domain(email_obj.get("sender_email", "")) != "amazon.com":
            continue
        body = email_obj.get("body_text", "")
        order_number = extract.extract_order_number(body, "amazon.com")
        if not is_hit(order_number):
            order_number = extract.extract_order_number(
                f"{email_obj.get('subject', '')} {body}",
                "amazon.com",
            )
        if not is_hit(order_number):
            continue

        item_blocks = amazon_item_blocks_by_order(body, order_number)
        if not item_blocks:
            distinct_body_orders = {match.group(1) for match in amazon_order_markers(extract.normalize_body(body))}
            if len(distinct_body_orders) <= 1:
                subject_item = extract.extractItemNameFromSubject(email_obj.get("subject", ""))
                if subject_item:
                    item_blocks = [{"item_name": subject_item["itemName"], "quantity": 1, "order_number": order_number}]
        flags = amazon_line_item_flags(email_obj.get("subject", ""))
        status = amazon_line_item_status(email_obj.get("subject", ""))
        parsed_email_date = extract.parse_email_date(email_obj.get("date", ""))
        email_date = parsed_email_date.isoformat() if parsed_email_date else ""
        message_id = email_obj.get("message_id") or email_obj.get("messageId") or ""

        if not item_blocks and any(flags.values()):
            for record in state.values():
                if record.get("order_number") == order_number:
                    record["payment_declined"] = record.get("payment_declined", False) or flags["payment_declined"]
                    record["shipping_delayed"] = record.get("shipping_delayed", False) or flags["shipping_delayed"]
                    record["last_seen_email_date"] = max_iso_date(record.get("last_seen_email_date", ""), email_date)
                    if message_id and message_id not in record["contributing_message_ids"]:
                        record["contributing_message_ids"].append(message_id)
            continue

        for item_block in item_blocks:
            item_order_number = item_block.get("order_number") or order_number
            normalized = extract.normalizeItemName(item_block["item_name"])["cleaned"]
            if not normalized:
                continue
            key = matching_line_item_key(state, item_order_number, normalized)
            record = state.setdefault(
                key,
                default_line_item_record(
                    item_order_number,
                    item_block["item_name"],
                    normalized,
                    item_block["quantity"],
                ),
            )
            if len(item_block["item_name"]) > len(record.get("item_name_raw", "")):
                record["item_name_raw"] = item_block["item_name"]
            record["quantity"] = max(int(record.get("quantity", 0)), item_block["quantity"])
            record["payment_declined"] = record.get("payment_declined", False) or flags["payment_declined"]
            record["shipping_delayed"] = record.get("shipping_delayed", False) or flags["shipping_delayed"]
            record["last_seen_email_date"] = max_iso_date(record.get("last_seen_email_date", ""), email_date)
            if message_id and message_id not in record["contributing_message_ids"]:
                record["contributing_message_ids"].append(message_id)
            advance_line_item_status(record, status, email_date)
    return state


def amazon_line_item_rows(state: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for record in state.values():
        rows.append(
            {
                **record,
                "num_contributing_emails": len(record.get("contributing_message_ids", [])),
            }
        )
    return sorted(rows, key=lambda row: (row["order_number"], row["item_name_normalized"]))


def write_amazon_line_items_csv(rows: list[dict[str, Any]], csv_path: str | Path) -> None:
    with Path(csv_path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AMAZON_LINE_ITEM_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in AMAZON_LINE_ITEM_CSV_FIELDS})


def load_amazon_line_item_state(path: str | Path = AMAZON_LINE_ITEM_STATE_PATH) -> dict[str, dict[str, Any]]:
    state_path = Path(path)
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


def write_amazon_line_item_state(
    state: dict[str, dict[str, Any]],
    path: str | Path = AMAZON_LINE_ITEM_STATE_PATH,
) -> None:
    Path(path).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def print_amazon_order_breakdown(rows: list[dict[str, Any]], order_number: str = VERIFICATION_ORDER_NUMBER) -> None:
    order_rows = [row for row in rows if row["order_number"] == order_number]
    print()
    print(f"Amazon line items for order {order_number}:")
    if not order_rows:
        print("No line items found.")
        return
    print(
        "order_number | item_name_normalized | quantity | current_status | "
        "ordered_date | shipped_date | delivered_date | added_to_inventory | num_contributing_emails"
    )
    print("--- | --- | ---: | --- | --- | --- | --- | --- | ---:")
    for row in order_rows:
        print(
            f"{row['order_number']} | {row['item_name_normalized']} | {row['quantity']} | "
            f"{row['current_status']} | {row['ordered_date']} | {row['shipped_date']} | "
            f"{row['delivered_date']} | {row['added_to_inventory']} | {row['num_contributing_emails']}"
        )


def build_result_row(email_obj: dict[str, Any]) -> dict[str, Any]:
    domain = extract.get_domain(email_obj.get("sender_email", ""))
    data = extract.extract_order_data(email_obj)
    message_id = email_obj.get("message_id") or email_obj.get("messageId") or ""
    return {
        "domain": domain,
        "status": infer_status(email_obj.get("subject", ""), email_obj.get("body_text", "")),
        "message_ids": message_id,
        "_message_id_list": [message_id] if message_id else [],
        "date": email_obj.get("date", ""),
        "subject": email_obj.get("subject", ""),
        "order_number": data["orderNumber"],
        "total": data["total"],
        "item_name": data["itemName"],
        "items": data["items"],
        "item_name_source": data["itemNameSource"],
        "item_name_normalized": data["itemNameNormalized"],
        "item_name_truncated": data["itemNameTruncated"],
        "item_name_words": data["itemNameWords"],
        "arrival_date": data["arrivalDate"],
        "arrival_date_source": data["arrivalDateSource"],
        "confidence": data["confidence"],
        "needs_review": data["needsReview"],
        "_body_text": email_obj.get("body_text", ""),
    }


def write_csv(rows: list[dict[str, Any]], csv_path: str | Path) -> None:
    with Path(csv_path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            output_row = {field: row[field] for field in CSV_FIELDS}
            output_row["items"] = " | ".join(row.get("items", []))
            writer.writerow(output_row)


def row_completeness(row: dict[str, Any]) -> int:
    return sum(
        [
            is_hit(row["order_number"]),
            is_hit(row["total"]),
            is_hit(row["item_name"]),
            is_hit(row["arrival_date"]),
            len(row.get("items", [])) > 1,
            not row.get("item_name_truncated", False),
        ]
    )


def best_row_for_field(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    def score(row: dict[str, Any]) -> tuple[int, int, int, int]:
        value_hit = int(is_hit(row.get(field)))
        item_count = len(row.get("items", []))
        not_truncated = int(not row.get("item_name_truncated", False))
        ordered_bonus = int(row.get("status") == "Ordered")
        status_rank = STATUS_RANK.get(row.get("status", "Unknown"), 0)
        if field == "arrival_date":
            return (value_hit, status_rank, row_completeness(row), item_count)
        if field in {"item_name", "items"}:
            return (value_hit, item_count, not_truncated, ordered_bonus)
        if field == "total":
            return (value_hit, ordered_bonus, row_completeness(row), item_count)
        return (value_hit, row_completeness(row), status_rank, item_count)

    return max(rows, key=score)


def merge_order_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) == 1:
        row = dict(rows[0])
        row["message_ids"] = ", ".join(row.get("_message_id_list", []))
        return row

    # Merge strategy: collapse rows sharing an order number into one purchase.
    # Choose item list/name and total from the most complete source, while
    # taking arrival date and status from the latest fulfillment-status email.
    canonical = dict(max(rows, key=row_completeness))
    item_source = best_row_for_field(rows, "items")
    total_source = best_row_for_field(rows, "total")
    arrival_source = best_row_for_field(rows, "arrival_date")
    latest_status_row = max(rows, key=lambda row: STATUS_RANK.get(row.get("status", "Unknown"), 0))

    canonical["item_name"] = item_source["item_name"]
    canonical["items"] = item_source.get("items", [])
    canonical["item_name_source"] = item_source["item_name_source"]
    canonical["item_name_truncated"] = item_source["item_name_truncated"]
    canonical["item_name_normalized"] = item_source["item_name_normalized"]
    canonical["item_name_words"] = item_source["item_name_words"]
    canonical["multiple_items"] = len(canonical["items"]) > 1
    canonical["total"] = total_source["total"]
    canonical["arrival_date"] = arrival_source["arrival_date"]
    canonical["arrival_date_source"] = arrival_source["arrival_date_source"]
    canonical["status"] = latest_status_row["status"]
    canonical["subject"] = item_source["subject"]
    canonical["message_ids"] = ", ".join(
        message_id
        for row in rows
        for message_id in row.get("_message_id_list", [])
        if message_id
    )

    hits = [
        is_hit(canonical["order_number"]),
        is_hit(canonical["total"]),
        is_hit(canonical["item_name"]),
        is_hit(canonical["arrival_date"]),
    ]
    canonical["confidence"] = round(sum(hits) / len(hits), 2)
    canonical["needs_review"] = canonical["confidence"] < 0.75
    return canonical


def collapse_duplicate_orders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows):
        if is_hit(row["order_number"]):
            key = f"order:{row['order_number']}"
        else:
            key = f"row:{index}"
        grouped[key].append(row)
    return [merge_order_group(group) for group in grouped.values()]


def print_summary(rows: list[dict[str, Any]]) -> None:
    domains: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        domains[row["domain"]].append(row)

    print("domain | count | order# hit rate | total hit rate | item name hit rate | arrival date hit rate")
    print("--- | ---: | ---: | ---: | ---: | ---:")

    sorted_domains = sorted(domains.items(), key=lambda item: len(item[1]), reverse=True)
    for domain, domain_rows in sorted_domains:
        count = len(domain_rows)
        order_hits = sum(is_hit(row["order_number"]) for row in domain_rows)
        total_hits = sum(is_hit(row["total"]) for row in domain_rows)
        item_hits = sum(is_hit(row["item_name"]) for row in domain_rows)
        arrival_hits = sum(is_hit(row["arrival_date"]) for row in domain_rows)
        print(
            f"{domain} | {count} | {format_rate(order_hits, count)} | "
            f"{format_rate(total_hits, count)} | {format_rate(item_hits, count)} | "
            f"{format_rate(arrival_hits, count)}"
        )

    print_low_hit_snippets(sorted_domains)


def print_low_hit_snippets(sorted_domains: list[tuple[str, list[dict[str, Any]]]]) -> None:
    for domain, domain_rows in sorted_domains:
        count = len(domain_rows)
        order_rate = sum(is_hit(row["order_number"]) for row in domain_rows) / count
        item_rate = sum(is_hit(row["item_name"]) for row in domain_rows) / count
        if order_rate >= 0.70 and item_rate >= 0.70:
            continue

        print()
        print(f"Examples for {domain} (order# or item hit rate below 70%):")
        for row in domain_rows[:3]:
            snippet = " ".join(row["_body_text"].split())[:300]
            print(f"- Subject: {row['subject']}")
            print(f"  Body: {snippet}")


def jaccard_similarity(left_words: list[str], right_words: list[str]) -> float:
    left = set(left_words)
    right = set(right_words)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _item_words_for_row(row: dict[str, Any]) -> list[str]:
    words = row.get("item_name_words")
    if isinstance(words, list):
        return [str(word) for word in words]
    normalized = row.get("item_name_normalized")
    if normalized:
        return str(normalized).split()
    item_name = row.get("item_name")
    if item_name:
        return extract.normalizeItemName(str(item_name))["words"]
    return []


def findSimilarItems(allRows: list[dict[str, Any]], threshold: float = 0.6) -> list[list[dict[str, Any]]]:
    parents = list(range(len(allRows)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    word_sets = [_item_words_for_row(row) for row in allRows]
    for left_index in range(len(allRows)):
        for right_index in range(left_index + 1, len(allRows)):
            if jaccard_similarity(word_sets[left_index], word_sets[right_index]) > threshold:
                union(left_index, right_index)

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(allRows):
        grouped[find(index)].append(row)

    clusters = [
        cluster
        for cluster in grouped.values()
        if len({row["domain"] for row in cluster}) > 1
    ]

    print("Similar item clusters across distinct domains:")
    if not clusters:
        print("No cross-domain clusters above similarity threshold.")
        return []

    for cluster_index, cluster in enumerate(clusters, start=1):
        domains = ", ".join(sorted({row["domain"] for row in cluster}))
        print(f"\nCluster {cluster_index}: {len(cluster)} rows across {domains}")
        for row in cluster[:10]:
            print(f"- {row['domain']} | {row.get('item_name', '')} | {row.get('subject', '')}")
    return clusters


def analyze_file(
    parsed_path: str | Path = "parsed_emails.json",
    csv_path: str | Path = "extraction_results.csv",
    print_report: bool = True,
    amazon_state_path: str | Path = AMAZON_LINE_ITEM_STATE_PATH,
    amazon_line_items_csv_path: str | Path = AMAZON_LINE_ITEM_CSV_PATH,
) -> list[dict[str, Any]]:
    emails = json.loads(Path(parsed_path).read_text(encoding="utf-8"))
    global LAST_CATEGORY_COUNTS, LAST_RAW_INVENTORY_COUNT
    LAST_CATEGORY_COUNTS = Counter()
    inventory_emails = []
    for email_obj in emails:
        category = classifyEmailCategory(
            email_obj.get("sender_email", ""),
            email_obj.get("sender_name", ""),
            email_obj.get("subject", ""),
            email_obj.get("body_text", ""),
        )
        LAST_CATEGORY_COUNTS[category] += 1
        if category == "inventory":
            inventory_emails.append(email_obj)

    LAST_RAW_INVENTORY_COUNT = len(inventory_emails)
    raw_rows = [build_result_row(email_obj) for email_obj in inventory_emails]
    rows = collapse_duplicate_orders(raw_rows)
    amazon_state = update_amazon_line_item_state(
        inventory_emails,
        load_amazon_line_item_state(amazon_state_path),
    )
    amazon_rows = amazon_line_item_rows(amazon_state)
    write_amazon_line_item_state(amazon_state, amazon_state_path)
    write_amazon_line_items_csv(amazon_rows, amazon_line_items_csv_path)
    write_csv(rows, csv_path)
    if print_report:
        print("Category counts:")
        print(f"inventory: {LAST_CATEGORY_COUNTS['inventory']}")
        print(f"food: {LAST_CATEGORY_COUNTS['food']}")
        print(f"ignore: {LAST_CATEGORY_COUNTS['ignore']}")
        print(f"Inventory distinct purchases written: {len(rows)}")
        print()
        print_summary(rows)
        print()
        print(f"Wrote {len(amazon_rows)} Amazon line items to {amazon_line_items_csv_path}")
        print_amazon_order_breakdown(amazon_rows)
        print()
        print(f"Wrote {len(rows)} rows to {csv_path}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze local order extraction hit rates by domain.")
    parser.add_argument(
        "--input",
        default="parsed_emails.json",
        help="Path to parsed email JSON from parse_mbox.py (default: parsed_emails.json)",
    )
    parser.add_argument(
        "--csv",
        default="extraction_results.csv",
        help="Path to write full extraction CSV (default: extraction_results.csv)",
    )
    parser.add_argument(
        "--similar-items",
        action="store_true",
        help="Print cross-domain item similarity clusters after writing the CSV",
    )
    parser.add_argument(
        "--attribution-audit",
        action="store_true",
        help="Print per-item cross-order attribution audit (true vs phantom)",
    )
    args = parser.parse_args()

    rows = analyze_file(args.input, args.csv)
    if args.attribution_audit:
        print()
        emails = json.loads(Path(args.input).read_text(encoding="utf-8"))
        inventory_emails = [
            email_obj
            for email_obj in emails
            if classifyEmailCategory(
                email_obj.get("sender_email", ""),
                email_obj.get("sender_name", ""),
                email_obj.get("subject", ""),
                email_obj.get("body_text", ""),
            )
            == "inventory"
        ]
        print_cross_order_attribution_audit(inventory_emails)
    if args.similar_items:
        print()
        findSimilarItems(rows)


if __name__ == "__main__":
    main()
