import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.gmail_api_url_batch_store import GmailApiUrlBatchStore
from core.qan8_gmail_api_client import Qan8Order, Qan8SourceRecord
from core.qan8_gmail_api_purchaser import Qan8GmailApiPurchaser


class GmailApiUrlPurchaserTests(unittest.TestCase):
    def test_purchase_materializes_one_source_in_canonical_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GmailApiUrlBatchStore(Path(temp_dir) / "turb.sqlite3")
            batch_id = store.create_empty_batch(
                target_count=12,
                aliases_per_source=12,
                desired_sources=1,
            )
            client = MagicMock()
            client.sku_id = "42"
            client.proxy_url = "http://proxy.example:8080"
            client.create_order.return_value = Qan8Order(
                order_no="gapi-order-1",
                status="completed",
                delivery="source@gmail.com----https://mail.example/source",
            )
            client.parse_delivery.return_value = [
                Qan8SourceRecord(
                    email="source@gmail.com",
                    code_url="https://mail.example/source",
                )
            ]

            purchaser = Qan8GmailApiPurchaser(
                client=client,
                poll_interval=0,
                order_timeout=1,
            )
            self.assertTrue(purchaser.purchase_source(batch_id, store=store))

            client.create_order.assert_called_once_with("gapi-" + batch_id[:16] + "-1", quantity=1)
            self.assertEqual(store.count_source_groups(batch_id), 1)
            self.assertEqual(store.batch_status(batch_id)["pending"], 12)
            orders = store.list_purchase_orders(batch_id)
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["status"], "completed")
            self.assertEqual(orders[0]["source_email"], "source@gmail.com")
            self.assertEqual(orders[0]["code_url"], "https://mail.example/source")

    def test_unknown_order_is_looked_up_without_posting_again(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GmailApiUrlBatchStore(Path(temp_dir) / "turb.sqlite3")
            batch_id = store.create_empty_batch(
                target_count=1,
                aliases_per_source=12,
                desired_sources=1,
            )
            order_no = "gapi-existing-order"
            store.create_purchase_order(batch_id, order_no, "42")
            store.update_purchase_order(batch_id, order_no, status="unknown")

            client = MagicMock()
            client.sku_id = "42"
            client.proxy_url = "http://proxy.example:8080"
            client.get_order.return_value = Qan8Order(
                order_no=order_no,
                status="completed",
                delivery="recovered@gmail.com----https://mail.example/recovered",
            )
            client.parse_delivery.return_value = [
                Qan8SourceRecord(
                    email="recovered@gmail.com",
                    code_url="https://mail.example/recovered",
                )
            ]

            purchaser = Qan8GmailApiPurchaser(
                client=client,
                poll_interval=0,
                order_timeout=1,
            )
            self.assertTrue(purchaser.purchase_source(batch_id, store=store))

            client.create_order.assert_not_called()
            client.get_order.assert_called_once_with(order_no)
            self.assertEqual(store.count_source_groups(batch_id), 1)


if __name__ == "__main__":
    unittest.main()
