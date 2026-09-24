import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


def _db_context(root: Path) -> ExitStack:
    stack = ExitStack()
    for target, value in (
        ("_ACCOUNTS_JSON", root / "accounts.json"),
        ("_ACCOUNTS_TXT", root / "accounts.txt"),
        ("_TOKENS_TXT", root / "tokens.txt"),
        ("_OUTLOOK_JSON", root / "outlook.json"),
        ("_OUTLOOK_TXT", root / "outlook.txt"),
        ("_LEGACY_ACCOUNTS_JSON", root / "legacy.json"),
        ("_LEGACY_OUTLOOK_JSON", root / "legacy-outlook.json"),
    ):
        stack.enter_context(patch.object(db, target, value))
    stack.enter_context(patch.object(db, "_schedule_static_viewer_refresh"))
    return stack


def _seed_plus_account_with_failed_recheck() -> int:
    account_id = db.insert_account(email="plus@example.com", access_token="token")
    db.update_account_plan_check(
        acc_id=account_id,
        result={
            "ok": True,
            "current_plan_type": "plus",
            "checked_at": "2026-09-17T10:00:00",
            "plus_trial_eligible": False,
        },
    )
    db.update_account_plan_check(
        acc_id=account_id,
        result={
            "ok": False,
            "error": "AT已过期/失效，请手动查活刷新",
            "http_status": 401,
            "token_expired": True,
            "token_expires_at": "2026-09-10T10:00:00+00:00",
        },
    )
    return account_id


class AccountPlanBadgeApiTests(unittest.TestCase):
    def test_plain_accounts_api_keeps_known_plan_and_last_success_on_failed_check(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            _db_context(Path(temp_dir)),
        ):
            account_id = _seed_plus_account_with_failed_recheck()
            app = create_app(auth_code="test-auth")
            response = app.test_client().get(
                "/api/accounts", headers={"X-Auth-Code": "test-auth"}
            )

            self.assertEqual(response.status_code, 200)
            items = response.get_json()
            target = next(item for item in items if item["id"] == account_id)
            self.assertEqual(target["plan_check_status"], "failed")
            self.assertEqual(target["current_plan_type"], "plus")
            self.assertTrue(target.get("plan_last_success_at"))
            self.assertTrue(target.get("plan_checked_at"))
            self.assertIn("AT已过期", target.get("plan_check_error") or "")

    def test_paged_accounts_api_includes_plan_success_fields_for_badge(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            _db_context(Path(temp_dir)),
        ):
            account_id = _seed_plus_account_with_failed_recheck()
            app = create_app(auth_code="test-auth")
            response = app.test_client().get(
                "/api/accounts?paged=1&page=1&page_size=50",
                headers={"X-Auth-Code": "test-auth"},
            )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            target = next(item for item in payload["items"] if item["id"] == account_id)
            self.assertEqual(target["current_plan_type"], "plus")
            self.assertTrue(target.get("plan_last_success_at"))
            self.assertTrue(target.get("plan_checked_at"))
            self.assertTrue(target.get("plan_check_error"))


class AccountPlanBadgeTemplateTests(unittest.TestCase):
    def test_plan_cell_renders_known_plan_alongside_failure_note(self):
        template_path = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        source = template_path.read_text(encoding="utf-8")

        # The failed branch must only take over when no plan is known.
        self.assertIn("plan !== '-'", source)
        self.assertIn("failureNote", source)
        # Known-plan rows show the plan pill plus a failure note line.
        self.assertIn("查询失败: ", source)
        # Failed plan cells offer the retry-login action that refreshes the AT
        # and chains a plan re-check.
        self.assertIn("Login lại lấy AT", source)


class ViPlanErrorTranslationTests(unittest.TestCase):
    """The vi.js dictionary must carry full-sentence entries for the plan-check
    error strings stored server-side, so the Gói column never shows raw Han."""

    def test_vi_js_translates_plan_check_error_sentences(self):
        vi_path = Path(__file__).resolve().parents[1] / "webui" / "static" / "vi.js"
        source = vi_path.read_text(encoding="utf-8")

        self.assertIn("'AT已过期/失效，请手动查活刷新'", source)
        self.assertIn("'WebUI 重启导致套餐查询中断，请重新查询'", source)
        self.assertIn("'上次套餐查询状态已超时，可重新查询'", source)
        self.assertIn("'查询失败'", source)
        self.assertIn("'套餐查询失败，未返回具体原因'", source)
        self.assertIn("'Login lại lấy AT'", source)


def _seed_pay153_link_account(email: str, kind: str, url: str | None) -> int:
    account_id = db.insert_account(email=email, access_token="token")
    db.update_account_pay153(
        acc_id=account_id,
        result={
            "ok": True,
            "status": "success",
            "checkout_session_kind": kind,
            "checkout_url": url,
            "link_type": "ph_short",
        },
    )
    return account_id


class AccountPay153KindFilterTests(unittest.TestCase):
    def test_db_list_accounts_filters_by_pay153_checkout_kind(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            _db_context(Path(temp_dir)),
        ):
            cs_live_id = _seed_pay153_link_account(
                "cs@example.com", "cs_live", "https://chatgpt.com/checkout/openai_llc/cs_live_x"
            )
            oaics_id = _seed_pay153_link_account(
                "oaics@example.com", "oaics", "https://chatgpt.com/checkout/openai_ie/oaics_x"
            )
            plain_id = db.insert_account(email="plain@example.com", access_token="token")

            cs_rows = db.list_accounts(pay153_kind_filter="cs_live")
            oaics_rows = db.list_accounts(pay153_kind_filter="oaics")
            unknown_rows = db.list_accounts(pay153_kind_filter="unknown")

        self.assertEqual([r["id"] for r in cs_rows], [cs_live_id])
        self.assertEqual([r["id"] for r in oaics_rows], [oaics_id])
        self.assertIn(plain_id, [r["id"] for r in unknown_rows])
        self.assertNotIn(cs_live_id, [r["id"] for r in unknown_rows])

    def test_paged_accounts_api_filters_by_pay153_kind_and_exposes_url(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            _db_context(Path(temp_dir)),
        ):
            cs_live_url = "https://chatgpt.com/checkout/openai_llc/cs_live_live1"
            cs_live_id = _seed_pay153_link_account("live@example.com", "cs_live", cs_live_url)
            _seed_pay153_link_account("custom@example.com", "oaics", "https://chatgpt.com/checkout/openai_ie/oaics_1")
            app = create_app(auth_code="test-auth")
            client = app.test_client()
            headers = {"X-Auth-Code": "test-auth"}

            response = client.get(
                "/api/accounts?paged=1&page=1&page_size=50&pay153_kind=cs_live",
                headers=headers,
            )
            filtered = response.get_json()
            self.assertEqual(
                [item["id"] for item in filtered["items"]], [cs_live_id]
            )
            self.assertEqual(filtered["items"][0]["pay153_checkout_session_kind"], "cs_live")
            self.assertEqual(filtered["items"][0]["pay153_checkout_url"], cs_live_url)

            response = client.get(
                "/api/accounts?paged=1&page=1&page_size=50&pay153_kind=oaics",
                headers=headers,
            )
            self.assertEqual(response.get_json()["total"], 1)

            ids_response = client.get(
                "/api/accounts/filtered-ids?pay153_kind=cs_live&limit=100",
                headers=headers,
            )
            self.assertEqual(ids_response.get_json()["account_ids"], [cs_live_id])


class AccountPay153UiTests(unittest.TestCase):
    def test_accounts_workspace_exposes_pay153_kind_filter_and_link_actions(self):
        template_path = Path(__file__).resolve().parents[1] / "webui" / "templates" / "index.html"
        source = template_path.read_text(encoding="utf-8")

        # Filter select wired through the standard account filter plumbing.
        self.assertIn('id="accountPay153KindFilterV2"', source)
        self.assertIn("pay153_kind: getAccountsPay153KindFilter()", source)
        self.assertIn("'accountPay153KindFilterV2', 'Link PAY.153'", source)
        # Per-row link badge with click-to-pay and copy actions.
        self.assertIn("function _pay153KindLine(r)", source)
        self.assertIn('rel="noopener">Thanh toán</a>', source)
        self.assertIn("Mở link PAY.153", source)
        self.assertIn("Copy link PAY.153", source)


if __name__ == "__main__":
    unittest.main()
