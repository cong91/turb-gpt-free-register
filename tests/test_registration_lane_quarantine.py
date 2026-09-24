import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config import email as email_config
from core import email_provider, registration_service
from core.gmail_api_url_client import GmailApiUrlError


class RegistrationLaneQuarantineTests(unittest.TestCase):
    def setUp(self):
        registration_service._STOP_EVENTS.clear()
        registration_service._ACTIVE_JOBS.clear()

    def tearDown(self):
        registration_service._STOP_EVENTS.clear()
        registration_service._ACTIVE_JOBS.clear()

    def test_gmail_api_url_602_cancels_jobs_sharing_code_url_batch(self):
        jobs = [
            {
                "id": 10,
                "status": "running",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-1",
                    "gmail_api_url_source_slot": 0,
                    "proxy_lane_id": 0,
                },
            },
            {
                "id": 11,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-1",
                    "gmail_api_url_source_slot": 0,
                    "proxy_lane_id": 0,
                },
            },
            {
                "id": 12,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-1",
                    "gmail_api_url_source_slot": 1,
                    "proxy_lane_id": 1,
                },
            },
        ]
        registration_service._ACTIVE_JOBS.add(10)
        registration_service._STOP_EVENTS[10] = threading.Event()

        with (
            patch.object(
                registration_service.db,
                "get_job",
                side_effect=lambda job_id: next(
                    (job for job in jobs if int(job["id"]) == int(job_id)),
                    None,
                ),
            ),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
            patch.object(
                registration_service.db,
                "fail_gmail_api_url_sources_for_code_url",
                create=True,
                return_value=1,
            ) as fail_source,
            patch(
                "core.gmail_api_url_client.quarantine_code_url",
                return_value=2,
            ) as quarantine_url,
            patch(
                "core.gmail_api_url_batch_store.GmailApiUrlBatchStore"
            ) as store_class,
        ):
            store_class.return_value.list_job_ids_for_code_url.return_value = ["10"]
            store_class.return_value.list_batch_ids_for_code_urls.return_value = ["batch-1"]
            store_class.return_value.source_slots_for_code_url.return_value = {0}
            store_class.return_value.cancel_waiter.return_value = True
            result = registration_service.quarantine_provider_lane(
                job_id=10,
                source="gmail_api_url",
                code_url="https://mail.example/broken",
                reason="Provider error code=602",
            )

        quarantine_url.assert_called_once_with(
            "https://mail.example/broken",
            reason="Provider error code=602",
        )
        fail_source.assert_called_once()
        self.assertEqual(fail_source.call_args.args[0], "https://mail.example/broken")
        self.assertEqual(fail_source.call_args.kwargs["note"], "Provider error code=602")
        self.assertFalse(registration_service._STOP_EVENTS[10].is_set())
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(result["stopping"], 0)
        update_job.assert_called_once_with(
            11,
            status="cancelled",
            error="邮箱来源已因 code_url 故障停止，取消同源任务: Provider error code=602",
            completed_at=unittest.mock.ANY,
        )
        store_class.return_value.cancel_waiter.assert_called_once_with(
            "batch-1",
            "11",
            "邮箱来源已因 code_url 故障停止，取消同源任务: Provider error code=602",
        )

    def test_gmail_api_url_602_does_not_cancel_unrelated_proxy_lane(self):
        jobs = [
            {
                "id": 30,
                "status": "running",
                "email_source": "gmail_api_url",
                "provider_context": {"proxy_lane_id": 0},
            },
            {
                "id": 31,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {"proxy_lane_id": 0},
            },
        ]
        registration_service._ACTIVE_JOBS.add(30)
        registration_service._STOP_EVENTS[30] = threading.Event()

        with (
            patch.object(registration_service.db, "get_job", return_value=jobs[0]),
            patch.object(registration_service.db, "update_job") as update_job,
            patch.object(
                registration_service.db,
                "fail_gmail_api_url_sources_for_code_url",
                create=True,
                return_value=1,
            ) as fail_source,
            patch(
                "core.gmail_api_url_client.quarantine_code_url",
                return_value=0,
            ) as quarantine_url,
            patch(
                "core.gmail_api_url_batch_store.GmailApiUrlBatchStore"
            ) as store_class,
        ):
            store_class.return_value.list_job_ids_for_code_url.return_value = ["30"]
            result = registration_service.quarantine_provider_lane(
                job_id=30,
                source="gmail_api_url",
                code_url="https://mail.example/broken",
                reason="Provider error code=602",
            )

        self.assertEqual(result["cancelled"], 0)
        self.assertEqual(result["stopping"], 0)
        quarantine_url.assert_called_once_with(
            "https://mail.example/broken",
            reason="Provider error code=602",
        )
        fail_source.assert_called_once()
        self.assertEqual(fail_source.call_args.args[0], "https://mail.example/broken")
        self.assertEqual(fail_source.call_args.kwargs["note"], "Provider error code=602")
        update_job.assert_not_called()

    def test_gmail_api_url_602_requests_stop_for_other_running_source_job(self):
        jobs = [
            {
                "id": 40,
                "status": "running",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-2",
                    "gmail_api_url_source_slot": 0,
                },
            },
            {
                "id": 41,
                "status": "running",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-2",
                    "gmail_api_url_source_slot": 0,
                },
            },
        ]
        registration_service._ACTIVE_JOBS.update({40, 41})
        registration_service._STOP_EVENTS[40] = threading.Event()
        registration_service._STOP_EVENTS[41] = threading.Event()

        with (
            patch.object(
                registration_service.db,
                "get_job",
                side_effect=lambda job_id: next(
                    (job for job in jobs if int(job["id"]) == int(job_id)),
                    None,
                ),
            ),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
            patch.object(
                registration_service.db,
                "fail_gmail_api_url_sources_for_code_url",
                create=True,
                return_value=1,
            ),
            patch("core.gmail_api_url_client.quarantine_code_url", return_value=2),
            patch("core.gmail_api_url_batch_store.GmailApiUrlBatchStore") as store_class,
        ):
            store_class.return_value.list_job_ids_for_code_url.return_value = ["40", "41"]
            result = registration_service.quarantine_provider_lane(
                job_id=40,
                source="gmail_api_url",
                code_url="https://mail.example/broken",
                reason="Provider error code=602",
            )

        self.assertEqual(result["stopping"], 1)
        self.assertTrue(registration_service._STOP_EVENTS[41].is_set())
        update_job.assert_called_once_with(
            41,
            status="stopping",
            error="邮箱来源已因 code_url 故障停止，取消同源任务: Provider error code=602",
        )

    def test_gmail_api_url_602_continues_cancelling_when_waiter_is_missing(self):
        jobs = [
            {
                "id": 60,
                "status": "running",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-3",
                    "gmail_api_url_source_slot": 0,
                },
            },
            {
                "id": 61,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-3",
                    "gmail_api_url_source_slot": 0,
                },
            },
            {
                "id": 62,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-3",
                    "gmail_api_url_source_slot": 0,
                },
            },
        ]
        registration_service._ACTIVE_JOBS.add(60)
        registration_service._STOP_EVENTS[60] = threading.Event()

        with (
            patch.object(
                registration_service.db,
                "get_job",
                side_effect=lambda job_id: next(
                    (job for job in jobs if int(job["id"]) == int(job_id)),
                    None,
                ),
            ),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
            patch.object(
                registration_service.db,
                "fail_gmail_api_url_sources_for_code_url",
                create=True,
                return_value=1,
            ),
            patch("core.gmail_api_url_client.quarantine_code_url", return_value=2),
            patch("core.gmail_api_url_batch_store.GmailApiUrlBatchStore") as store_class,
        ):
            store_class.return_value.list_job_ids_for_code_url.return_value = ["60"]
            store_class.return_value.list_batch_ids_for_code_urls.return_value = ["batch-3"]
            store_class.return_value.source_slots_for_code_url.return_value = {0}
            store_class.return_value.cancel_waiter.side_effect = RuntimeError(
                "waiter was never registered"
            )

            result = registration_service.quarantine_provider_lane(
                job_id=60,
                source="gmail_api_url",
                code_url="https://mail.example/broken",
                reason="Provider error code=602",
            )

        self.assertEqual(result["cancelled"], 2)
        self.assertEqual(
            [call.args[0] for call in update_job.call_args_list],
            [61, 62],
        )
        self.assertEqual(store_class.return_value.cancel_waiter.call_count, 2)

    def test_gmail_api_url_602_uses_each_batch_alias_capacity_for_source_slots(self):
        jobs = [
            {
                "id": 70,
                "status": "running",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-4",
                    "gmail_api_url_source_slot": 0,
                    "gmail_api_url_aliases_per_source": 12,
                },
            },
            {
                "id": 71,
                "status": "pending",
                "email_source": "gmail_api_url",
                "provider_context": {
                    "gmail_api_url_batch_id": "batch-5",
                    "gmail_api_url_source_slot": 1,
                    "gmail_api_url_aliases_per_source": 6,
                },
            },
        ]
        registration_service._ACTIVE_JOBS.add(70)
        registration_service._STOP_EVENTS[70] = threading.Event()

        def source_slots(batch_id, _code_url, *, aliases_per_source):
            if batch_id == "batch-4":
                self.assertEqual(aliases_per_source, 12)
                return {0}
            self.assertEqual(batch_id, "batch-5")
            self.assertEqual(aliases_per_source, 6)
            return {1}

        with (
            patch.object(
                registration_service.db,
                "get_job",
                side_effect=lambda job_id: next(
                    (job for job in jobs if int(job["id"]) == int(job_id)),
                    None,
                ),
            ),
            patch.object(registration_service.db, "list_jobs", return_value=jobs),
            patch.object(registration_service.db, "update_job") as update_job,
            patch.object(
                registration_service.db,
                "fail_gmail_api_url_sources_for_code_url",
                create=True,
                return_value=1,
            ),
            patch("core.gmail_api_url_client.quarantine_code_url", return_value=2),
            patch("core.gmail_api_url_batch_store.GmailApiUrlBatchStore") as store_class,
        ):
            store_class.return_value.list_job_ids_for_code_url.return_value = ["70"]
            store_class.return_value.list_batch_ids_for_code_urls.return_value = [
                "batch-4",
                "batch-5",
            ]
            store_class.return_value.batch_provision_plan.side_effect = (
                lambda batch_id: {
                    "aliases_per_source": 12 if batch_id == "batch-4" else 6
                }
            )
            store_class.return_value.source_slots_for_code_url.side_effect = source_slots
            store_class.return_value.cancel_waiter.return_value = True

            result = registration_service.quarantine_provider_lane(
                job_id=70,
                source="gmail_api_url",
                code_url="https://mail.example/broken",
                reason="Provider error code=602",
            )

        self.assertEqual(result["cancelled"], 1)
        update_job.assert_called_once_with(
            71,
            status="cancelled",
            error="邮箱来源已因 code_url 故障停止，取消同源任务: Provider error code=602",
            completed_at=unittest.mock.ANY,
        )

    def test_snapshot_code_602_quarantines_the_provider_source(self):
        account = SimpleNamespace(
            email="alias@gmail.com",
            code_url="https://mail.example/broken",
            batch_id="batch-gmail",
            lane_id=2,
        )
        with (
            patch.object(email_provider, "resolve_email_source", return_value="gmail_api_url"),
            patch.object(email_provider, "_get_code_url_account", return_value=account),
            patch(
                "core.gmail_api_url_client.snapshot_verification_code",
                side_effect=GmailApiUrlError("Provider error code=602"),
            ),
            patch.object(registration_service, "quarantine_provider_lane") as quarantine,
            patch.object(registration_service._THREAD_CTX, "job_id", 77, create=True),
            self.assertRaisesRegex(GmailApiUrlError, "code=602"),
        ):
            email_provider.snapshot_verification_code(
                "alias@gmail.com",
                stage="registration_email_request",
            )

        quarantine.assert_called_once_with(
            job_id=77,
            source="gmail_api_url",
            code_url="https://mail.example/broken",
            provider_batch_id="batch-gmail",
            provider_lane_id=2,
            reason="Provider error code=602",
        )

    def test_gmail_api_url_quarantines_on_code_602(self):
        account = SimpleNamespace(email="alias@gmail.com", code_url="https://mail.example/gmail")
        with (
            patch.object(email_config, "USE_EMAIL_SERVICE", True),
            patch.object(email_provider, "resolve_email_source", return_value="gmail_api_url"),
            patch.object(email_provider, "_get_code_url_account", return_value=account),
            patch(
                "core.gmail_api_url_client.poll_verification_code",
                side_effect=GmailApiUrlError("Provider error code=602: expired"),
            ),
            patch.object(registration_service, "quarantine_provider_lane") as quarantine,
            patch.object(registration_service._THREAD_CTX, "job_id", 77, create=True),
            self.assertRaises(GmailApiUrlError),
        ):
            email_provider.wait_for_otp("alias@gmail.com", after_ts=1.0)

        quarantine.assert_called_once_with(
            job_id=77,
            source="gmail_api_url",
            code_url=account.code_url,
            provider_batch_id=None,
            provider_lane_id=None,
            reason="Provider error code=602: expired",
        )

    def test_timeout_does_not_quarantine_url_lane(self):
        account = SimpleNamespace(email="alias@gmail.com", code_url="https://mail.example/code")
        with (
            patch.object(email_config, "USE_EMAIL_SERVICE", True),
            patch.object(email_provider, "resolve_email_source", return_value="gmail_api_url"),
            patch.object(email_provider, "_get_code_url_account", return_value=account),
            patch(
                "core.gmail_api_url_client.poll_verification_code",
                side_effect=GmailApiUrlError("Timeout after 60s waiting for new OTP"),
            ),
            patch.object(registration_service, "quarantine_provider_lane") as quarantine,
            patch.object(registration_service._THREAD_CTX, "job_id", 77, create=True),
            self.assertRaisesRegex(GmailApiUrlError, "Timeout"),
        ):
            email_provider.wait_for_otp("alias@gmail.com", after_ts=1.0)

        quarantine.assert_not_called()


if __name__ == "__main__":
    unittest.main()
