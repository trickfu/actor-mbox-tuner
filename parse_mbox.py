#!/usr/bin/env python3
"""Parse an mbox export into local JSON for order-email extraction tuning."""

from __future__ import annotations

import argparse
import html
import json
import mailbox
import re
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from pathlib import Path
from typing import Iterable


ORDER_SUBJECT_RE = re.compile(r"\b(order|receipt|confirmation|shipped|shipping)\b", re.I)
EXCLUDE_SUBJECT_RE = re.compile(
    r"\b(unsubscribe|subscription|email preferences|mailing list|newsletter)\b",
    re.I,
)


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def decode_part_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw_payload = part.get_payload()
        return raw_payload if isinstance(raw_payload, str) else ""

    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?is)<br\s*/?>", "\n", value)
    value = re.sub(r"(?is)</p\s*>", "\n", value)
    value = re.sub(r"(?is)<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n\s+", "\n", value)
    return value.strip()


def iter_body_parts(message: Message) -> Iterable[Message]:
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            disposition = (part.get_content_disposition() or "").lower()
            if disposition == "attachment":
                continue
            yield part
    else:
        yield message


def extract_body_text(message: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in iter_body_parts(message):
        content_type = part.get_content_type().lower()
        if content_type == "text/plain":
            plain_parts.append(decode_part_payload(part))
        elif content_type == "text/html":
            html_parts.append(decode_part_payload(part))

    if plain_parts:
        return "\n".join(text.strip() for text in plain_parts if text.strip()).strip()
    if html_parts:
        return html_to_text("\n".join(html_parts))
    return ""


def looks_like_order_email(subject: str) -> bool:
    subject = subject or ""
    return bool(ORDER_SUBJECT_RE.search(subject)) and not EXCLUDE_SUBJECT_RE.search(subject)


def parse_message(message: Message) -> dict[str, str]:
    sender_name, sender_email = parseaddr(decode_header_value(message.get("From")))
    return {
        "message_id": decode_header_value(message.get("Message-ID")),
        "sender_email": sender_email,
        "sender_name": sender_name,
        "subject": decode_header_value(message.get("Subject")),
        "date": decode_header_value(message.get("Date")),
        "body_text": extract_body_text(message),
    }


def parse_mbox_file(
    mbox_path: str | Path,
    output_path: str | Path = "parsed_emails.json",
) -> tuple[int, int]:
    mbox = mailbox.mbox(mbox_path)
    parsed_messages: list[dict[str, str]] = []
    total = 0

    try:
        for message in mbox:
            total += 1
            parsed = parse_message(message)
            if looks_like_order_email(parsed["subject"]):
                parsed_messages.append(parsed)
    finally:
        mbox.close()

    output = Path(output_path)
    output.write_text(json.dumps(parsed_messages, indent=2, ensure_ascii=False), encoding="utf-8")
    return total, len(parsed_messages)


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse order-like emails from a local mbox file.")
    parser.add_argument("mbox_path", help="Path to the .mbox file exported from your mail client")
    parser.add_argument(
        "--output",
        default="parsed_emails.json",
        help="Path to write parsed JSON output (default: parsed_emails.json)",
    )
    args = parser.parse_args()

    total, filtered = parse_mbox_file(args.mbox_path, args.output)
    print(f"Total messages in mbox: {total}")
    print(f"Order-like messages written: {filtered}")


if __name__ == "__main__":
    main()
