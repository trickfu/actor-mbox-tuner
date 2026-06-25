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
        self.assertTrue(result["needsReview"])

    def test_normalize_item_name_builds_stable_key(self):
        result = extract.normalizeItemName(
            "Premium New 2025 100pcs USBGear 10-Port USB Hub 3.2, Heavy Duty x2"
        )

        self.assertEqual(result["cleaned"], "usbgear 10 port usb hub 3")
        self.assertEqual(result["words"], ["usbgear", "10", "port", "usb", "hub", "3"])

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

            rows = analyze.analyze_file(parsed_path, csv_path, print_report=False)

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

            rows = analyze.analyze_file(parsed_path, csv_path, print_report=False)
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

            rows = analyze.analyze_file(parsed_path, csv_path, print_report=False)
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
