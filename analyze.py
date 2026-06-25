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
    "confidence",
    "needs_review",
]

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
STATUS_RANK = {"Unknown": 0, "Ordered": 1, "Shipped": 2, "Delivered": 3}
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
    sender_name = senderName or ""
    subject_text = subject or ""
    combined = f"{subject_text} {bodyText or ''}"

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
    text = f"{subject or ''} {body_text or ''}"
    if re.search(r"\bdelivered\b", text, re.I):
        return "Delivered"
    if re.search(r"\b(shipped|shipping|out for delivery|delivery update|delivery estimate update|on the way|tracking)\b", text, re.I):
        return "Shipped"
    if re.search(r"\bordered\b", text, re.I):
        return "Ordered"
    return "Unknown"


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
    args = parser.parse_args()

    rows = analyze_file(args.input, args.csv)
    if args.similar_items:
        print()
        findSimilarItems(rows)


if __name__ == "__main__":
    main()
