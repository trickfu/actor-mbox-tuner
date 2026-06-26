# Noridoc: Actor Mbox Tuner

Path: @/

### Overview

- This repository contains local-only Python tooling for testing order email extraction against an exported `.mbox` before the extraction patterns are ported elsewhere.
- The workflow flows from `@/parse_mbox.py` into `@/parsed_emails.json`, then through `@/analyze.py` and `@/extract.py` into human-review CSV/JSON outputs.
- Amazon inventory tracking is line-item based because a single Amazon order can produce multiple fulfillment emails and a single Amazon email can describe multiple order numbers.

### How it fits into the larger codebase

- `@/parse_mbox.py` is the ingestion boundary. It reads local mailbox messages, decodes sender/subject/date/message/body fields, strips invisible characters through extraction helpers in `@/extract.py`, and writes the parsed order-like message stream consumed by `@/analyze.py`.
- `@/analyze.py` is the inventory boundary. It classifies parsed messages, excludes non-inventory flows, writes broad extraction review rows, and maintains Amazon line-item state in `@/amazon_line_items.json` for later review through `@/amazon_line_items.csv`.
- `@/extract.py` owns the regex-compatible extraction primitives. It normalizes body text, extracts order numbers, totals, item names, Amazon body item blocks, and parsed dates for the analysis pipeline.
- The generated files `@/parsed_emails.json`, `@/extraction_results.csv`, `@/amazon_line_items.json`, and `@/amazon_line_items.csv` are workflow outputs, not source-of-truth implementation files.

### Core Implementation

- `@/parse_mbox.py` filters messages by subject before analysis. Order, fulfillment, delivery, and ordered-item subjects are retained so Amazon status emails can update inventory readiness even when they are not conventional receipt subjects.
- `@/analyze.py` routes each parsed message through `classifyEmailCategory()` before detailed extraction. Only inventory messages become inventory review rows; food delivery and ignored messages stop before line-item tracking.
- Amazon item extraction starts with body bullet blocks matched by `@/extract.py` as `* <item name> Quantity: <n>` or `* <item name> Qty: <n>`. `extractAmazonItemBlocksWithPositionsFromBody()` returns each cleaned block with its position in the normalized body so `@/analyze.py` can associate the block with an order marker that appeared earlier in the same email.
- `amazon_item_blocks_by_order()` in `@/analyze.py` treats `orderID=<amazon order number>` URL query markers as the source of truth for multi-order Amazon bodies. Each item block is assigned to the nearest preceding `orderID=` marker; visible `Order #` markers are used only when no URL order markers exist, and the primary extracted order number is used for blocks that appear before any marker or for bodies with no markers.
- Amazon line-item records in `@/analyze.py` are keyed by `order_number + item_name_normalized`. Each record carries quantity, status dates, contributing message IDs, payment/shipping flags, and whether a delivered item has been added to inventory.
- Status transitions in `@/analyze.py` are monotonic across `Ordered`, `Shipped`, `Out for delivery`, and `Delivered`; delivered records are marked inventory-ready once through the same state update path.
- Same-order Amazon item-name variants are merged by `matching_line_item_key()` in `@/analyze.py` when normalized tokens indicate the same product through subset matching, high token overlap, or a stable shared prefix. Cable, cord, and wire variants are canonicalized for this matching path.
- ASIN-based keying is only used when ASIN values are available in parsed Amazon bodies. When parsed bodies do not contain ASINs, the state remains keyed by order number and normalized item name with the same-order token-overlap merge behavior.

### Things to Know

- `@/analyze.py` normalizes the email body before marker and item-block scanning, so marker positions and item-block positions are measured in the same text representation.
- In multi-order Amazon emails, visible `Order #` text may not be the attribution source used for line items when `orderID=` URL markers are present. URL order markers take precedence for every positioned item block in that body.
- Subject-derived Amazon item names are a fallback in `@/analyze.py` for emails that lack body bullet quantity blocks; those fallback items stay attached to the primary order extracted from the message.
- `@/extract.py` rejects UI button labels and SKU-only candidates before Amazon item blocks enter line-item state, keeping navigational text out of inventory records.
- `@/test_local_tools.py` contains regression coverage for URL marker attribution, visible `Order #` fallback behavior, same-order normalized-name merging, subject filtering, and the generated Amazon line-item CSV shape.

Created and maintained by Nori.
