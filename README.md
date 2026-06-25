# Local Mbox Order Extraction Tuner

This repo contains local-only Python tooling for testing regex-based order email extraction against a real `.mbox` export before porting finalized patterns back into Google Apps Script.

No script connects to Gmail, Google Sheets, or any Google API. The workflow reads a local mbox file and writes local JSON/CSV files.

## First Run

Use Python 3.11+ if available. There are no external dependencies.

```bash
python3 parse_mbox.py /path/to/your-export.mbox
python3 analyze.py
```

`parse_mbox.py` writes `parsed_emails.json` and prints:

```text
Total messages in mbox: 1234
Order-like messages written: 87
```

`analyze.py` reads `parsed_emails.json`, classifies emails as `inventory`, `food`, or `ignore`, writes inventory-only `extraction_results.csv`, and updates Amazon line-item tracking files: `amazon_line_items.json` for persistent state and `amazon_line_items.csv` for review.

```text
Category counts:
inventory: 204
food: 23
ignore: 4
Inventory distinct purchases written: 130

domain | count | order# hit rate | total hit rate | item name hit rate | arrival date hit rate
--- | ---: | ---: | ---: | ---: | ---:
amazon.com | 42 | 95% | 88% | 64% | 71%
etsy.com | 12 | 83% | 92% | 75% | 25%

Examples for amazon.com (order# or item hit rate below 70%):
- Subject: Your Amazon.com order has shipped
  Body: Thanks for your order. Arriving June 25 ...

Wrote 54 rows to extraction_results.csv
```

Open `extraction_results.csv` in a spreadsheet to manually scan every parsed order-like email and identify wrong or missing extractions. For Amazon inventory readiness, use `amazon_line_items.csv`; Amazon orders are tracked by line item rather than order number because one order can contain items that ship and deliver independently.

The CSV includes the raw extracted `item_name` for human review, `items` for multi-item orders, an `item_name_source` label showing whether the selected name came from Amazon body bullets, subject text, generic body parsing, or no match, an `item_name_normalized` key for rough product matching, and `item_name_truncated` to flag subject-line Amazon product titles that ended with an ellipsis. It also includes `arrival_date_source`, which distinguishes confirmed delivered-email dates from parsed delivery estimates. Amazon item extraction checks body `* ... Quantity:` / `* ... Qty:` blocks before falling back to quoted Amazon status subjects and then generic body parsing.

The legacy inventory CSV still collapses duplicate emails by `order_number` for broad extraction review. Amazon inventory tracking now uses line items keyed by `order_number + item_name_normalized`; grouped and individual Amazon emails for the same physical item update the same tracked line item. When two normalized Amazon item names on the same order have a subset relationship, `analyze.py` treats them as the same line item and keeps the richer normalized name, allowing short subject-fallback records to merge into later body-derived item records.

## Extraction Flow

`parse_mbox.py` does the broad local mbox pass: it decodes sender, subject, date, `Message-ID`, and body text, strips invisible Unicode control characters that can break regex matching, then writes subject-level order-like and fulfillment-status messages to `parsed_emails.json`. Delivered and out-for-delivery subjects are intentionally included because Amazon line-item inventory readiness depends on those emails.

`analyze.py` performs the inventory boundary before running detailed extraction. `classifyEmailCategory()` uses sender, subject, and body text to route each parsed message to `inventory`, `food`, or `ignore`; only inventory messages become CSV rows. Food delivery senders and DoorDash-style food order subjects are excluded before item, total, and arrival extraction runs.

`extract.py` owns field extraction. For Amazon, body bullet quantity blocks like `* <item name> Quantity: <n>` are the primary line-item source; when those blocks are absent, older quoted Amazon order/shipping subjects are used as a narrow fallback. Generic item extraction rejects UI button text and SKU-only labels such as `Item no.: 8105207`.

`normalizeItemName()` preserves identifying size/spec tokens such as `M2.5`, `16mm`, `12V`, `200W`, `1/2"`, `100pcs`, and pack counts after removing configurable marketing filler words.

`analyze.py` maintains a persistent Amazon line-item state record for each `order_number + item_name_normalized`. Each record tracks quantity, current status, ordered/shipped/delivered dates, contributing message IDs, payment/shipping flags, and `added_to_inventory`. Status only advances (`Ordered` -> `Shipped` -> `Out for delivery` -> `Delivered`) and delivered line items are marked inventory-ready once.

## Iteration Loop

Run `parse_mbox.py` once per mbox export:

```bash
python3 parse_mbox.py /path/to/your-export.mbox
```

Then repeat this loop until the hit rates and CSV review look acceptable:

1. Edit sender-specific or generic regex patterns in `extract.py`.
2. Run `python3 analyze.py`.
3. Check the per-domain hit rates sorted by highest email volume.
4. Review the printed raw body snippets for domains below 70% order-number or item-name hit rate.
5. Open `extraction_results.csv` for general extraction review and `amazon_line_items.csv` for Amazon line-item inventory status.
6. Adjust patterns in `extract.py` and re-run `analyze.py`.

When a domain has lots of volume or poor hit rates, add sender-specific entries to `SENDER_RULES` in `extract.py`. Keep those patterns close to Apps Script-compatible regular expressions so they can be ported back to `Extractor.gs` with minimal changes.

To review likely duplicate/similar products across different retailers, run:

```bash
python3 analyze.py --similar-items
```

This prints clusters from `findSimilarItems()` using token overlap on `item_name_normalized`. It is a review report only; it does not merge or deduplicate rows.

## Files

- `parse_mbox.py`: Reads a local `.mbox`, extracts message ID/sender/subject/date/body text, filters order-like subjects, and writes `parsed_emails.json`.
- `extract.py`: Contains the Apps Script-style extraction functions and editable regex tables.
- `analyze.py`: Runs extraction on parsed emails, prints domain-level hit rates, writes `extraction_results.csv`, and updates `amazon_line_items.json` / `amazon_line_items.csv`.
- `test_local_tools.py`: Lightweight standard-library tests for the local workflow.
