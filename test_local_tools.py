import email
import io
import csv
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import analyze
import extract
import parse_mbox


class ParseMboxTests(unittest.TestCase):
    def test_order_filter_includes_order_subjects_and_excludes_unsubscribe(self):
        self.assertTrue(parse_mbox.looks_like_order_email("Your order has shipped"))
        self.assertTrue(parse_mbox.looks_like_order_email("Receipt for your purchase"))
        self.assertTrue(parse_mbox.looks_like_order_email('Ordered: 5 "USBGear 10-Port USB Hub..." and 34 more items'))
        self.assertTrue(parse_mbox.looks_like_order_email('Delivered: "Widget Pro" and 2 more items'))
        self.assertTrue(parse_mbox.looks_like_order_email('Out for delivery: "Widget Pro"'))
        self.assertFalse(parse_mbox.looks_like_order_email("Unsubscribe confirmation"))
        self.assertFalse(parse_mbox.looks_like_order_email("Weekly newsletter"))

    def test_message_parser_prefers_plain_text_and_falls_back_to_html(self):
        raw = (
            "From: Amazon <shipment-tracking@amazon.com>\n"
            "Subject: Your order has shipped\n"
            "Date: Tue, 23 Jun 2026 10:00:00 -0700\n"
            "MIME-Version: 1.0\n"
            "Content-Type: multipart/alternative; boundary=abc\n\n"
            "--abc\n"
            "Content-Type: text/html; charset=utf-8\n\n"
            "<html><body><p>HTML body</p></body></html>\n"
            "--abc\n"
            "Content-Type: text/plain; charset=utf-8\n\n"
            "Plain body\n"
            "--abc--\n"
        )
        msg = email.message_from_string(raw)

        parsed = parse_mbox.parse_message(msg)

        self.assertEqual(parsed["sender_email"], "shipment-tracking@amazon.com")
        self.assertEqual(parsed["sender_name"], "Amazon")
        self.assertEqual(parsed["subject"], "Your order has shipped")
        self.assertEqual(parsed["body_text"], "Plain body")


class ExtractTests(unittest.TestCase):
    def test_extract_order_data_for_amazon_like_body(self):
        email_obj = {
            "sender_email": "shipment-tracking@amazon.com",
            "sender_name": "Amazon",
            "subject": "Your Amazon.com order #123-4567890-1234567 has shipped",
            "date": "Tue, 23 Jun 2026 10:00:00 -0700",
            "body_text": (
                "Order # 123-4567890-1234567\n"
                "Arriving June 25, 2026\n"
                "View or manage order\n"
                "Noise\n"
                "Wireless Keyboard\n"
                "Qty: 1\n"
                "Order Total: $49.99\n"
            ),
        }

        result = extract.extract_order_data(email_obj)

        self.assertEqual(result["orderNumber"], "123-4567890-1234567")
        self.assertEqual(result["store"], "Amazon")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["total"], "49.99")
        self.assertEqual(result["itemName"], "Wireless Keyboard")
        self.assertEqual(result["arrivalDate"], "2026-06-25")
        self.assertFalse(result["needsReview"])

    def test_extract_total_handles_amazon_amount_then_currency_format(self):
        result = extract.extract_order_data(
            {
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Shipped: "Widget Pro"',
                "date": "Tue, 23 Jun 2026 10:00:00 -0700",
                "body_text": "Order # 123-4567890-1234567\n* Widget Pro Quantity: 1 88.77 USD Total 96.42 USD",
            }
        )

        self.assertEqual(result["total"], "96.42")
        self.assertEqual(result["currency"], "USD")

    def test_amazon_item_name_comes_from_subject_before_body_buttons(self):
        email_obj = {
            "sender_email": "shipment-tracking@amazon.com",
            "sender_name": "Amazon",
            "subject": 'Your Amazon.com order of "USBGear 10-Port USB Hub 3.2..." and 1 more item has shipped!',
            "date": "Tue, 23 Jun 2026 10:00:00 -0700",
            "body_text": "Item: Track package\nOrder Total: $49.99",
        }

        result = extract.extract_order_data(email_obj)

        self.assertEqual(result["itemName"], "USBGear 10-Port USB Hub 3.2")
        self.assertTrue(result["multipleItems"])
        self.assertTrue(result["itemNameTruncated"])

    def test_amazon_body_bullet_items_beat_truncated_subject_and_capture_all_items(self):
        result = extract.extract_order_data(
            {
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Ordered: "Pisichen Portable..."',
                "date": "Tue, 23 Jun 2026 10:00:00 -0700",
                "body_text": (
                    "Order # 123-4567890-1234567\n"
                    "* Pisichen Portable Touchscreen Monitor 16 Inch 1920X1200P IPS Touch Screen Monitor "
                    "with USB C HDMI Port Quantity: 1\n"
                    "* USB-C Cable 10ft Qty: 2\n"
                    "Order Total: $149.99"
                ),
            }
        )

        self.assertEqual(
            result["itemName"],
            "Pisichen Portable Touchscreen Monitor 16 Inch 1920X1200P IPS Touch Screen Monitor with USB C HDMI Port",
        )
        self.assertEqual(
            result["items"],
            [
                "Pisichen Portable Touchscreen Monitor 16 Inch 1920X1200P IPS Touch Screen Monitor with USB C HDMI Port",
                "USB-C Cable 10ft",
            ],
        )
        self.assertTrue(result["multipleItems"])
        self.assertFalse(result["itemNameTruncated"])

    def test_amazon_status_subject_fallback_marks_truncated(self):
        result = extract.extract_order_data(
            {
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Delivery estimate update: "240 pcs M5 T Nut Screws Kit..."',
                "date": "Tue, 23 Jun 2026 10:00:00 -0700",
                "body_text": "Order # 123-4567890-1234567\nOrder Total: $12.99",
            }
        )

        self.assertEqual(result["itemName"], "240 pcs M5 T Nut Screws Kit")
        self.assertTrue(result["itemNameTruncated"])

    def test_amazon_subject_strips_leading_quantity_prefixes(self):
        examples = [
            ('Your Amazon.com order of 5x "Widget Pro" has shipped', "Widget Pro"),
            ('Your Amazon.com order of "2" x PATIKIL Silicone O-Ring has shipped', "PATIKIL Silicone O-Ring"),
            ('Shipped: 3 "Cable Clips" and 2 more items', "Cable Clips"),
        ]

        for subject, expected in examples:
            with self.subTest(subject=subject):
                result = extract.extract_order_data(
                    {
                        "sender_email": "shipment-tracking@amazon.com",
                        "sender_name": "Amazon",
                        "subject": subject,
                        "date": "Tue, 23 Jun 2026 10:00:00 -0700",
                        "body_text": "Item: View order",
                    }
                )
                self.assertEqual(result["itemName"], expected)

    def test_body_item_extraction_rejects_ui_button_phrases(self):
        result = extract.extract_order_data(
            {
                "sender_email": "orders@example.com",
                "sender_name": "Example Store",
                "subject": "Order confirmation",
                "date": "Tue, 23 Jun 2026 10:00:00 -0700",
                "body_text": "Order # ABC12345\nItem: Track package\nOrder total: $10.00",
            }
        )

        self.assertEqual(result["itemName"], extract.NOT_FOUND)
        self.assertEqual(result["items"], [])
        self.assertTrue(result["needsReview"])

    def test_body_item_extraction_rejects_sku_only_labels(self):
        result = extract.extract_order_data(
            {
                "sender_email": "orders@acehardware.com",
                "sender_name": "Ace Hardware",
                "subject": "Order Received",
                "date": "Tue, 23 Jun 2026 10:00:00 -0700",
                "body_text": "Order number is 72703661\nItem: Item no.: 8105207\nOrder total: $10.00",
            }
        )

        self.assertEqual(result["itemName"], extract.NOT_FOUND)

    def test_normalize_item_name_builds_stable_key(self):
        result = extract.normalizeItemName(
            "Premium New 2025 100pcs USBGear 10-Port USB Hub 3.2, Heavy Duty x2"
        )

        for token in ["usbgear", "port", "hub", "100pcs", "3.2"]:
            self.assertIn(token, result["words"])

    def test_normalize_item_name_preserves_late_spec_tokens(self):
        result = extract.normalizeItemName(
            'HANGLIFE Heat-Set Threaded Inserts Assortment Kit for 3D Printing, M2.5, M3, 16mm, 12V, 1/2", 200W'
        )

        self.assertIn("m2.5", result["words"])
        self.assertIn("m3", result["words"])
        self.assertIn("16mm", result["words"])
        self.assertIn("12v", result["words"])
        self.assertIn('1/2"', result["words"])
        self.assertIn("200w", result["words"])
        for token in ["m2.5", "m3", "16mm", "12v", '1/2"', "200w"]:
            self.assertIn(token, result["cleaned"])

    def test_analysis_writes_csv_with_extracted_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parsed_path = Path(tmpdir) / "parsed_emails.json"
            csv_path = Path(tmpdir) / "extraction_results.csv"
            parsed_path.write_text(
                json.dumps(
                    [
                        {
                            "sender_email": "orders@mail.etsy.com",
                            "sender_name": "Etsy",
                            "subject": "Receipt for your Etsy order",
                            "date": "Tue, 23 Jun 2026 10:00:00 -0700",
                            "body_text": "Order # 1234567890\nOrder total: $12.50\nItem: Ceramic Mug",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            rows = analyze.analyze_file(
                parsed_path,
                csv_path,
                print_report=False,
                amazon_state_path=Path(tmpdir) / "amazon_line_items.json",
                amazon_line_items_csv_path=Path(tmpdir) / "amazon_line_items.csv",
            )

            self.assertEqual(rows[0]["domain"], "etsy.com")
            self.assertEqual(rows[0]["order_number"], "1234567890")
            self.assertEqual(rows[0]["item_name_normalized"], "ceramic mug")
            self.assertIn("Ceramic Mug", csv_path.read_text(encoding="utf-8"))

    def test_category_filter_excludes_food_and_ignore_before_csv_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parsed_path = Path(tmpdir) / "parsed_emails.json"
            csv_path = Path(tmpdir) / "extraction_results.csv"
            parsed_path.write_text(
                json.dumps(
                    [
                        {
                            "message_id": "<food@example>",
                            "sender_email": "no-reply@doordash.com",
                            "sender_name": "DoorDash Order",
                            "subject": "Order Confirmation for Jack from Gott's Roadside",
                            "date": "Tue, 23 Jun 2026 10:00:00 -0700",
                            "body_text": "Order # FOOD12345\nItem: Burger\nOrder Total: $18.50",
                        },
                        {
                            "message_id": "<ignore@example>",
                            "sender_email": "deals@rockler.com",
                            "sender_name": "Rockler",
                            "subject": "Deals and free shipping today",
                            "date": "Tue, 23 Jun 2026 10:00:00 -0700",
                            "body_text": "Order # DEAL12345\nItem: Marketing Widget\nOrder Total: $5.00",
                        },
                        {
                            "message_id": "<inventory@example>",
                            "sender_email": "shipment-tracking@amazon.com",
                            "sender_name": "Amazon",
                            "subject": 'Ordered: "Widget Pro"',
                            "date": "Tue, 23 Jun 2026 10:00:00 -0700",
                            "body_text": "Order # 123-4567890-1234567\n* Widget Pro Quantity: 1\nOrder Total: $10.00",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            rows = analyze.analyze_file(
                parsed_path,
                csv_path,
                print_report=False,
                amazon_state_path=Path(tmpdir) / "amazon_line_items.json",
                amazon_line_items_csv_path=Path(tmpdir) / "amazon_line_items.csv",
            )
            csv_text = csv_path.read_text(encoding="utf-8")

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["domain"], "amazon.com")
            self.assertNotIn("doordash.com", csv_text)
            self.assertNotIn("rockler.com", csv_text)
            self.assertNotIn("Burger", csv_text)
            self.assertNotIn("Marketing Widget", csv_text)

    def test_duplicate_order_numbers_collapse_to_canonical_inventory_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parsed_path = Path(tmpdir) / "parsed_emails.json"
            csv_path = Path(tmpdir) / "extraction_results.csv"
            parsed_path.write_text(
                json.dumps(
                    [
                        {
                            "message_id": "<ordered@example>",
                            "sender_email": "shipment-tracking@amazon.com",
                            "sender_name": "Amazon",
                            "subject": 'Ordered: "Widget Pro..."',
                            "date": "Tue, 23 Jun 2026 09:00:00 -0700",
                            "body_text": (
                                "Order # 123-4567890-1234567\n"
                                "* Widget Pro Full Product Name Quantity: 1\n"
                                "* Cable Kit Quantity: 1\n"
                                "Order Total: $39.99"
                            ),
                        },
                        {
                            "message_id": "<shipped@example>",
                            "sender_email": "shipment-tracking@amazon.com",
                            "sender_name": "Amazon",
                            "subject": 'Shipped: "Widget Pro..."',
                            "date": "Tue, 23 Jun 2026 12:00:00 -0700",
                            "body_text": "Order # 123-4567890-1234567\nArriving June 25, 2026",
                        },
                        {
                            "message_id": "<delivered@example>",
                            "sender_email": "shipment-tracking@amazon.com",
                            "sender_name": "Amazon",
                            "subject": 'Delivered: "Widget Pro..."',
                            "date": "Wed, 24 Jun 2026 12:00:00 -0700",
                            "body_text": "Order # 123-4567890-1234567\nArrives June 24, 2026",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            rows = analyze.analyze_file(
                parsed_path,
                csv_path,
                print_report=False,
                amazon_state_path=Path(tmpdir) / "amazon_line_items.json",
                amazon_line_items_csv_path=Path(tmpdir) / "amazon_line_items.csv",
            )
            with csv_path.open(newline="", encoding="utf-8") as handle:
                written_rows = list(csv.DictReader(handle))

            self.assertEqual(len(rows), 1)
            self.assertEqual(len(written_rows), 1)
            self.assertEqual(rows[0]["item_name"], "Widget Pro Full Product Name")
            self.assertEqual(rows[0]["total"], "39.99")
            self.assertEqual(rows[0]["arrival_date"], "2026-06-24")
            self.assertEqual(rows[0]["status"], "Delivered")
            self.assertEqual(
                set(rows[0]["message_ids"].split(", ")),
                {"<ordered@example>", "<shipped@example>", "<delivered@example>"},
            )
            self.assertEqual(written_rows[0]["status"], "Delivered")
            self.assertIn("<delivered@example>", written_rows[0]["message_ids"])

    def test_merge_preserves_total_from_ordered_email_when_delivered_email_lacks_total(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parsed_path = Path(tmpdir) / "parsed_emails.json"
            csv_path = Path(tmpdir) / "extraction_results.csv"
            parsed_path.write_text(
                json.dumps(
                    [
                        {
                            "message_id": "<ordered-total@example>",
                            "sender_email": "shipment-tracking@amazon.com",
                            "sender_name": "Amazon",
                            "subject": 'Ordered: "Widget Pro"',
                            "date": "Tue, 23 Jun 2026 09:00:00 -0700",
                            "body_text": (
                                "Order # 123-4567890-7654321\n"
                                "* Widget Pro Quantity: 1\n"
                                "Order Total: $88.77"
                            ),
                        },
                        {
                            "message_id": "<delivered-no-total@example>",
                            "sender_email": "shipment-tracking@amazon.com",
                            "sender_name": "Amazon",
                            "subject": 'Delivered: "Widget Pro"',
                            "date": "Wed, 24 Jun 2026 12:34:00 -0700",
                            "body_text": "Order # 123-4567890-7654321\nDelivered today",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            rows = analyze.analyze_file(
                parsed_path,
                csv_path,
                print_report=False,
                amazon_state_path=Path(tmpdir) / "amazon_line_items.json",
                amazon_line_items_csv_path=Path(tmpdir) / "amazon_line_items.csv",
            )
            with csv_path.open(newline="", encoding="utf-8") as handle:
                written_rows = list(csv.DictReader(handle))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["total"], "88.77")
            self.assertEqual(rows[0]["status"], "Delivered")
            self.assertEqual(rows[0]["arrival_date"], "2026-06-24")
            self.assertEqual(rows[0]["arrival_date_source"], "delivered_email")
            self.assertEqual(written_rows[0]["arrival_date_source"], "delivered_email")

    def test_invisible_unicode_is_stripped_before_subject_item_parsing(self):
        result = extract.extract_order_data(
            {
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Delivered: "\u20662\u2069x Widget\u00ad Pro"',
                "date": "Wed, 24 Jun 2026 12:34:00 -0700",
                "body_text": "Order # 123-4567890-7654321",
            }
        )

        self.assertEqual(result["itemName"], "2x Widget Pro")

    def test_delivered_email_date_is_used_as_arrival_date(self):
        result = extract.extract_order_data(
            {
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Delivered: "Widget Pro"',
                "date": "Wed, 24 Jun 2026 12:34:00 -0700",
                "body_text": "Order # 123-4567890-7654321\nDelivered today",
            }
        )

        self.assertEqual(result["arrivalDate"], "2026-06-24")
        self.assertEqual(result["arrivalDateSource"], "delivered_email")

    def test_amazon_line_items_dedup_grouped_and_individual_emails(self):
        emails = [
            {
                "message_id": "<grouped@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Delivered: "USBGear 10-Port USB Hub..." and 1 more item',
                "date": "Tue, 24 Jun 2026 10:00:00 -0700",
                "body_text": (
                    "Order # 114-2441697-7949852\n"
                    "* USBGear 10-Port USB Hub 3.2 with 12V Power Supply Quantity: 1\n"
                    "* ALITOVE 100pcs WS2812B LED Strip 5V Quantity: 2\n"
                ),
            },
            {
                "message_id": "<individual@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Shipped: "USBGear 10-Port USB Hub..."',
                "date": "Mon, 23 Jun 2026 09:00:00 -0700",
                "body_text": (
                    "Order # 114-2441697-7949852\n"
                    "* USBGear 10-Port USB Hub 3.2 with 12V Power Supply Quantity: 1\n"
                ),
            },
        ]

        state = analyze.update_amazon_line_item_state(emails, {})
        rows = analyze.amazon_line_item_rows(state)

        self.assertEqual(len(rows), 2)
        hub = next(row for row in rows if "usbgear" in row["item_name_normalized"])
        self.assertEqual(hub["current_status"], "Delivered")
        self.assertEqual(hub["quantity"], 1)
        self.assertEqual(hub["shipped_date"], "2026-06-23")
        self.assertEqual(hub["delivered_date"], "2026-06-24")
        self.assertTrue(hub["added_to_inventory"])
        self.assertEqual(hub["num_contributing_emails"], 2)

    def test_amazon_line_item_status_never_regresses_and_info_flags_do_not_advance(self):
        existing = {
            "114-2441697-7949852::widget pro m3": {
                "order_number": "114-2441697-7949852",
                "item_name_raw": "Widget Pro M3",
                "item_name_normalized": "widget pro m3",
                "quantity": 1,
                "current_status": "Delivered",
                "ordered_date": "2026-06-20",
                "shipped_date": "2026-06-21",
                "delivered_date": "2026-06-22",
                "last_seen_email_date": "2026-06-22",
                "contributing_message_ids": ["<delivered@example>"],
                "added_to_inventory": True,
            }
        }
        emails = [
            {
                "message_id": "<late-shipped@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Shipped: "Widget Pro M3"',
                "date": "Wed, 24 Jun 2026 09:00:00 -0700",
                "body_text": "Order # 114-2441697-7949852\n* Widget Pro M3 Quantity: 1",
            },
            {
                "message_id": "<delay@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": "Delay in shipping your order #114-2441697-7949852",
                "date": "Thu, 25 Jun 2026 09:00:00 -0700",
                "body_text": "Order # 114-2441697-7949852\n* Widget Pro M3 Quantity: 1",
            },
        ]

        state = analyze.update_amazon_line_item_state(emails, existing)
        row = analyze.amazon_line_item_rows(state)[0]

        self.assertEqual(row["current_status"], "Delivered")
        self.assertEqual(row["delivered_date"], "2026-06-22")
        self.assertTrue(row["shipping_delayed"])
        self.assertTrue(row["added_to_inventory"])

    def test_amazon_line_item_handles_out_for_delivery_and_payment_declined(self):
        emails = [
            {
                "message_id": "<ordered@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Ordered: "Jameco Wire"',
                "date": "Sat, 20 Jun 2026 09:00:00 -0700",
                "body_text": "Order # 114-2441697-7949852\n* Jameco Wire 22AWG Quantity: 1",
            },
            {
                "message_id": "<payment@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": "Payment Declined for order #114-2441697-7949852",
                "date": "Sat, 20 Jun 2026 10:00:00 -0700",
                "body_text": "Order # 114-2441697-7949852\n* Jameco Wire 22AWG Quantity: 1",
            },
            {
                "message_id": "<out@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Out for delivery: "Jameco Wire"',
                "date": "Mon, 22 Jun 2026 08:00:00 -0700",
                "body_text": "Order # 114-2441697-7949852\n* Jameco Wire 22AWG Quantity: 1",
            },
        ]

        row = analyze.amazon_line_item_rows(analyze.update_amazon_line_item_state(emails, {}))[0]

        self.assertEqual(row["current_status"], "Out for delivery")
        self.assertEqual(row["ordered_date"], "2026-06-20")
        self.assertEqual(row["payment_declined"], True)
        self.assertFalse(row["added_to_inventory"])

    def test_amazon_line_item_key_includes_order_number_and_normalized_item(self):
        emails = [
            {
                "message_id": "<first-order@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Delivered: "USBGear Hub"',
                "date": "Tue, 24 Jun 2026 10:00:00 -0700",
                "body_text": "Order # 111-1111111-1111111\n* USBGear Hub 3.2 Quantity: 1",
            },
            {
                "message_id": "<second-order@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Delivered: "USBGear Hub"',
                "date": "Tue, 24 Jun 2026 10:00:00 -0700",
                "body_text": "Order # 222-2222222-2222222\n* USBGear Hub 3.2 Quantity: 1",
            },
        ]

        rows = analyze.amazon_line_item_rows(analyze.update_amazon_line_item_state(emails, {}))

        self.assertEqual(len(rows), 2)
        self.assertEqual({row["order_number"] for row in rows}, {"111-1111111-1111111", "222-2222222-2222222"})

    def test_amazon_line_item_uses_old_subject_format_when_body_has_no_item_blocks(self):
        emails = [
            {
                "message_id": "<old-format@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Your Amazon.com order of "ALITOVE 100pcs WS2812B..." has shipped!',
                "date": "Tue, 26 May 2026 08:23:04 +0000",
                "body_text": "Order #114-2441697-7949852",
            }
        ]

        rows = analyze.amazon_line_item_rows(analyze.update_amazon_line_item_state(emails, {}))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_status"], "Shipped")
        self.assertIn("alitove", rows[0]["item_name_normalized"])
        self.assertIn("100pcs", rows[0]["item_name_normalized"])

    def test_amazon_line_item_merges_subject_fallback_with_full_body_name(self):
        emails = [
            {
                "message_id": "<subject-fallback@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Your Amazon.com order of "ALITOVE 100pcs WS2812B..." has shipped!',
                "date": "Tue, 26 May 2026 08:23:04 +0000",
                "body_text": "Order #114-2441697-7949852",
            },
            {
                "message_id": "<full-body@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Delivered: "ALITOVE 100pcs WS2812B..."',
                "date": "Thu, 28 May 2026 08:23:04 +0000",
                "body_text": (
                    "Order #114-2441697-7949852\n"
                    "* ALITOVE 100pcs WS2812B Addressable 5050 smart RGB LED Pixel Light 5V Quantity: 1"
                ),
            },
        ]

        rows = analyze.amazon_line_item_rows(analyze.update_amazon_line_item_state(emails, {}))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_status"], "Delivered")
        self.assertIn("addressable", rows[0]["item_name_normalized"])
        self.assertEqual(rows[0]["num_contributing_emails"], 2)

    def test_amazon_line_item_merges_same_order_prefix_key_variants(self):
        emails = [
            {
                "message_id": "<short-power@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Shipped: "Amazon Basics PC Power Cord..."',
                "date": "Tue, 24 Jun 2026 10:00:00 -0700",
                "body_text": "Order # 114-1816216-0000000\n* Amazon Basics PC Power Cord 3 Quantity: 1",
            },
            {
                "message_id": "<long-power@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Delivered: "Amazon Basics PC Power Cable..."',
                "date": "Wed, 25 Jun 2026 10:00:00 -0700",
                "body_text": "Order # 114-1816216-0000000\n* Amazon Basics PC Power Cable 6 Quantity: 1",
            },
        ]

        rows = analyze.amazon_line_item_rows(analyze.update_amazon_line_item_state(emails, {}))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_status"], "Delivered")
        self.assertEqual(rows[0]["num_contributing_emails"], 2)

    def test_amazon_line_item_merges_same_order_spec_subset_variants(self):
        emails = [
            {
                "message_id": "<short-edge@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Shipped: "Edge Supply Birch..."',
                "date": "Tue, 24 Jun 2026 10:00:00 -0700",
                "body_text": 'Order # 114-0777813-0000000\n* Edge Supply Birch 5/8" Quantity: 1',
            },
            {
                "message_id": "<long-edge@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Delivered: "Edge Supply Birch..."',
                "date": "Wed, 25 Jun 2026 10:00:00 -0700",
                "body_text": 'Order # 114-0777813-0000000\n* Edge Supply Birch x 25 Roll 5/8" Quantity: 1',
            },
        ]

        rows = analyze.amazon_line_item_rows(analyze.update_amazon_line_item_state(emails, {}))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_status"], "Delivered")
        self.assertIn('5/8"', rows[0]["item_name_normalized"])

    def test_amazon_line_item_assigns_multi_order_manifest_blocks_to_nearest_order(self):
        emails = [
            {
                "message_id": "<multi-order@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Ordered: 5 "USBGear..." and 34 more items',
                "date": "Tue, 26 May 2026 01:00:33 +0000",
                "body_text": (
                    "Order # 111-1111111-1111111\n"
                    "* Facmogu DC 12V Adapter Quantity: 10\n"
                    "* WAGO 221 Lever Nuts Quantity: 1\n"
                    "Grand Total: $100.00\n"
                    "Order # 222-2222222-2222222\n"
                    "* MECCANIXITY M5 Thumb Screws Quantity: 4\n"
                    "Grand Total: $20.00\n"
                    "Order # 114-2441697-7949852\n"
                    "* USBGear 10-Port USB Hub 3.2 Quantity: 5\n"
                    "* ALITOVE 100pcs WS2812B LED Strip 5V Quantity: 1\n"
                ),
            }
        ]

        rows = analyze.amazon_line_item_rows(analyze.update_amazon_line_item_state(emails, {}))
        target_rows = [row for row in rows if row["order_number"] == "114-2441697-7949852"]

        self.assertEqual(len(rows), 5)
        self.assertEqual(len(target_rows), 2)
        self.assertTrue(all("facmogu" not in row["item_name_normalized"] for row in target_rows))

    def test_amazon_line_item_prefers_order_id_url_markers_for_block_assignment(self):
        emails = [
            {
                "message_id": "<multi-order-url@example>",
                "sender_email": "shipment-tracking@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Ordered: 2 "Mixed order..."',
                "date": "Tue, 26 May 2026 01:00:33 +0000",
                "body_text": (
                    "Order # 999-9999999-9999999\n"
                    "https://www.amazon.com/your-orders/order-details?orderID=111-1111111-1111111&ref_=x\n"
                    "* Facmogu DC 12V Adapter Quantity: 10\n"
                    "https://www.amazon.com/your-orders/order-details?orderID=222-2222222-2222222&ref_=x\n"
                    "* WAGO 221 Lever Nuts Quantity: 1\n"
                ),
            }
        ]

        rows = analyze.amazon_line_item_rows(analyze.update_amazon_line_item_state(emails, {}))

        self.assertEqual({row["order_number"] for row in rows}, {"111-1111111-1111111", "222-2222222-2222222"})
        self.assertEqual(next(row for row in rows if "facmogu" in row["item_name_normalized"])["order_number"], "111-1111111-1111111")
        self.assertEqual(next(row for row in rows if "wago" in row["item_name_normalized"])["order_number"], "222-2222222-2222222")

    def test_amazon_subject_fallback_skipped_for_multi_order_confirmation(self):
        emails = [
            {
                "message_id": "<multi-order-noblocks@example>",
                "sender_email": "auto-confirm@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Your Amazon.com order of "Edge Supply Birch 5/8 X..." and 1 more item.',
                "date": "Tue, 26 May 2026 01:00:33 +0000",
                "body_text": (
                    "Hello, Thank you for your order. "
                    "Order # 114-0807316-0777813 "
                    "https://www.amazon.com/your-orders/order-details?orderID=114-0807316-0777813&ref_=x "
                    "Order # 114-0959342-5149854 "
                    "https://www.amazon.com/your-orders/order-details?orderID=114-0959342-5149854&ref_=x "
                    "Your order will arrive soon."
                ),
            }
        ]

        rows = analyze.amazon_line_item_rows(analyze.update_amazon_line_item_state(emails, {}))

        self.assertEqual(rows, [])

    def test_amazon_subject_fallback_used_for_single_order_confirmation(self):
        emails = [
            {
                "message_id": "<single-order-noblocks@example>",
                "sender_email": "auto-confirm@amazon.com",
                "sender_name": "Amazon",
                "subject": 'Your Amazon.com order of "Tan QY USB 3.0 Cable A Male...".',
                "date": "Tue, 26 May 2026 01:00:33 +0000",
                "body_text": (
                    "Hello, Thank you for your order. "
                    "Order # 114-9841725-6704204 "
                    "https://www.amazon.com/your-orders/order-details?orderID=114-9841725-6704204&ref_=x "
                    "Your order will arrive soon."
                ),
            }
        ]

        rows = analyze.amazon_line_item_rows(analyze.update_amazon_line_item_state(emails, {}))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order_number"], "114-9841725-6704204")

    def test_amazon_line_item_csv_contains_requested_columns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "amazon_line_items.csv"
            state = analyze.update_amazon_line_item_state(
                [
                    {
                        "message_id": "<ordered@example>",
                        "sender_email": "shipment-tracking@amazon.com",
                        "sender_name": "Amazon",
                        "subject": 'Ordered: "XIITIA Converter..."',
                        "date": "Sat, 20 Jun 2026 09:00:00 -0700",
                        "body_text": "Order # 114-2441697-7949852\n* XIITIA HDMI Converter 16mm Quantity: 3",
                    }
                ],
                {},
            )

            analyze.write_amazon_line_items_csv(analyze.amazon_line_item_rows(state), csv_path)
            with csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(
                list(rows[0].keys()),
                [
                    "order_number",
                    "item_name_normalized",
                    "quantity",
                    "current_status",
                    "ordered_date",
                    "shipped_date",
                    "delivered_date",
                    "added_to_inventory",
                    "num_contributing_emails",
                ],
            )
            self.assertEqual(rows[0]["current_status"], "Ordered")

    def test_find_similar_items_reports_cross_domain_clusters(self):
        rows = [
            {
                "domain": "amazon.com",
                "subject": "A",
                "item_name": "Premium USBGear 10 Port USB Hub",
                "item_name_normalized": "usbgear 10 port usb hub",
                "item_name_words": ["usbgear", "10", "port", "usb", "hub"],
            },
            {
                "domain": "example.com",
                "subject": "B",
                "item_name": "USBGear 10-Port USB Hub 3.2",
                "item_name_normalized": "usbgear 10 port usb hub",
                "item_name_words": ["usbgear", "10", "port", "usb", "hub"],
            },
            {
                "domain": "amazon.com",
                "subject": "C",
                "item_name": "Ceramic Mug",
                "item_name_normalized": "ceramic mug",
                "item_name_words": ["ceramic", "mug"],
            },
        ]

        output = io.StringIO()
        with redirect_stdout(output):
            clusters = analyze.findSimilarItems(rows)

        self.assertEqual(len(clusters), 1)
        self.assertIn("amazon.com", output.getvalue())
        self.assertIn("example.com", output.getvalue())


class MboxIntegrationTests(unittest.TestCase):
    def test_parse_mbox_file_writes_only_filtered_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mbox_path = Path(tmpdir) / "sample.mbox"
            output_path = Path(tmpdir) / "parsed_emails.json"
            mbox_path.write_text(
                "From sender@example.com Tue Jun 23 10:00:00 2026\n"
                "From: Store <orders@example.com>\n"
                "Subject: Order confirmation\n\n"
                "Thanks for your order.\n\n"
                "From sender@example.com Tue Jun 23 10:01:00 2026\n"
                "From: News <news@example.com>\n"
                "Subject: Weekly newsletter\n\n"
                "Hello.\n",
                encoding="utf-8",
            )

            total, filtered = parse_mbox.parse_mbox_file(mbox_path, output_path)

            parsed = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(total, 2)
            self.assertEqual(filtered, 1)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0]["subject"], "Order confirmation")


if __name__ == "__main__":
    unittest.main()
