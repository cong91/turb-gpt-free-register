# -*- coding: utf-8 -*-
"""Regression: mỗi CDK trong 1 thời điểm chỉ 1 worker tạo account."""
import threading
import time
import unittest
from unittest.mock import patch, Mock

from core import paymesh_mail_client as client
from core.paymesh_mail_client import PaymeshMailAccount, PaymeshMailError
from core.provider_card_ledger import ProviderCardLedger


class PerCdkLockTests(unittest.TestCase):
    def test_second_pick_same_cdk_blocks_until_first_releases(self):
        """Hai thread gọi pick_account cùng CDK; thread thứ 2 phải chờ thread 1 release."""
        results = []
        lock = threading.Lock()

        def slow_redeem(cdk, **kw):
            time.sleep(0.3)
            return PaymeshMailAccount("user@example.com", cdk, 6)

        def pick_thread(name):
            try:
                acct = client.pick_account(name, ["CARD-LOCK"])
                with lock:
                    results.append((name, "picked", time.time(), acct.email))
                time.sleep(0.4)
                if name == "first":
                    client.mark_account_consumed(acct.email)
            except Exception as exc:
                with lock:
                    results.append((name, "error", time.time(), str(exc)[:80]))

        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        with patch("core.paymesh_mail_client.redeem_cdk", side_effect=slow_redeem):
            with patch("core.paymesh_mail_client._config_values", return_value=("http://x", 30, 6)):
                import tempfile
                from pathlib import Path
                with tempfile.TemporaryDirectory() as td:
                    client._LEDGER = ProviderCardLedger(Path(td) / "ledger.json", "paymesh")
                    t1 = threading.Thread(target=pick_thread, args=("first",))
                    t2 = threading.Thread(target=pick_thread, args=("second",))
                    t1.start()
                    t2.start()
                    t1.join(timeout=10)
                    t2.join(timeout=10)

        names = [r[0] for r in results if r[1] == "picked"]
        self.assertEqual(names, ["first", "second"])
        first_time = [r[2] for r in results if r[0] == "first" and r[1] == "picked"][0]
        second_time = [r[2] for r in results if r[0] == "second" and r[1] == "picked"][0]
        # Thread 2 must pick AFTER thread 1 releases (not concurrent)
        self.assertGreater(second_time - first_time, 0.3,
                           f"second pick must wait for first to finish; gap={second_time - first_time}")
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        client._LEDGER = None

    def test_redeem_error_releases_cdk_lock(self):
        """Lỗi redeem không được làm khóa CDK vĩnh viễn cho worker tiếp theo."""
        client._CONTEXT_CACHE.clear()
        client._SEEN_CODES_BY_CDK.clear()
        client._CDK_LOCKS.clear()
        with patch("core.paymesh_mail_client._config_values", return_value=("http://x", 30, 6)):
            with patch("core.paymesh_mail_client.redeem_cdk", side_effect=RuntimeError("network down")):
                with self.assertRaises(RuntimeError):
                    client.pick_account("failed", ["CARD-ERROR"])
        self.assertFalse(client._get_cdk_lock("CARD-ERROR").locked())
        client._CDK_LOCKS.clear()
        client._LEDGER = None

    def test_consume_false_still_releases_cdk_lock(self):
        """Nếu ledger không chuyển trạng thái, context cũ vẫn phải nhả lock."""
        account = PaymeshMailAccount("stale@example.com", "CARD-STALE", 6, job_id="job")
        client._CONTEXT_CACHE[client._cache_key(account.email)] = account
        cdk_lock = client._get_cdk_lock(account.cdk)
        self.assertTrue(cdk_lock.acquire(blocking=False))
        with patch.object(client._ledger(), "consume", return_value=False):
            self.assertFalse(client.mark_account_consumed(account.email))
        self.assertIsNone(client.get_account_context(account.email))
        self.assertFalse(cdk_lock.locked())
        client._CONTEXT_CACHE.clear()
        client._CDK_LOCKS.clear()
        client._LEDGER = None


if __name__ == "__main__":
    unittest.main()