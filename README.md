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

`analyze.py` reads `parsed_emails.json`, classifies emails as `inventory`, `food`, or `ignore`, writes inventory-only `extraction_results.csv`, and prints a per-domain summary like:

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

Open `extraction_results.csv` in a spreadsheet to manually scan every parsed order-like email and identify wrong or missing extractions.

The CSV includes the raw extracted `item_name` for human review, `items` for multi-item orders, an `item_name_source` label showing whether the selected name came from Amazon body bullets, subject text, generic body parsing, or no match, an `item_name_normalized` key for rough product matching, and `item_name_truncated` to flag subject-line Amazon product titles that ended with an ellipsis. Amazon item extraction checks body `* ... Quantity:` / `* ... Qty:` blocks before falling back to quoted Amazon status subjects and then generic body parsing.

Duplicate inventory emails are collapsed by `order_number` before CSV writing. The merged row keeps a `status` value, comma-separated `message_ids`, item/total data from the most complete source, and arrival data from the strongest delivery/shipping source.

## Extraction Flow

`parse_mbox.py` does the broad local mbox pass: it decodes sender, subject, date, `Message-ID`, and body text, then writes only subject-level order-like messages to `parsed_emails.json`.

`analyze.py` performs the inventory boundary before running detailed extraction. `classifyEmailCategory()` uses sender, subject, and body text to route each parsed message to `inventory`, `food`, or `ignore`; only inventory messages become CSV rows. Food delivery senders and DoorDash-style food order subjects are excluded before item, total, and arrival extraction runs.

`extract.py` owns field extraction. For Amazon, `extract_order_data()` treats body bullet quantity blocks as the primary item source and preserves every matched item in `items`; when those blocks are absent, it falls back to quoted Amazon order/shipping/delivery subjects and records whether the selected name was truncated.

`analyze.py` then merges status emails that share an order number into a single canonical purchase row. The parser-provided message IDs remain attached as `message_ids` so the CSV preserves which source emails contributed to the merged inventory purchase.

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
5. Open `extraction_results.csv` and scan for wrong or missing values.
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
- `analyze.py`: Runs extraction on parsed emails, prints domain-level hit rates, shows example snippets for weak domains, and writes `extraction_results.csv`.
- `test_local_tools.py`: Lightweight standard-library tests for the local workflow.
