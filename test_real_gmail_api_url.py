# -*- coding: utf-8 -*-
"""
Test THẬT với email/URL do user cung cấp.
KHÔNG chạy full registration - chỉ test phần Gmail API URL polling.
"""
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")  # bỏ SSL warning
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from core import db
from core import email_provider
from core.gmail_api_url_client import (
    GmailApiUrlAccount,
    poll_verification_code,
    pick_account,
    get_account_context,
    release_account,
    GmailApiUrlError,
)

EMAIL    = "willjacob6442@gmail.com"
CODE_URL = "https://gapi.mailsapi.com/api/get-code?uid=sdceb05c12ab70e6bcd"


def sep(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ──────────────────────────────────────────────────────────────
# STEP 1: Gọi thẳng URL, kiểm tra response format
# ──────────────────────────────────────────────────────────────
def step_1_raw_api():
    sep("STEP 1 — Gọi thẳng API URL, kiểm tra format")
    print(f"URL: {CODE_URL}\n")

    resp = requests.get(CODE_URL, timeout=15, verify=False)
    print(f"HTTP status : {resp.status_code}")
    print(f"Raw body    : {resp.text}")

    data = resp.json()
    code = data.get("code")
    otp  = (data.get("data") or {}).get("code")
    msg  = data.get("message", "")

    print(f"\n→ code    = {code}  (0=success | 601=waiting | 602=error)")
    print(f"→ OTP     = {otp}")
    print(f"→ message = {msg}")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    if code == 0:
        print(f"\n✅ API trả SUCCESS với OTP = {otp}")
        assert otp is not None, "code=0 nhưng data.code bị null!"
    elif code == 601:
        print("\n⏳ API trả WAITING (601) — email chưa nhận được OTP")
    elif code == 602:
        print("\n❌ API trả ERROR (602) — provider lỗi, cần refund")
    else:
        print(f"\n⚠️  code lạ: {code}")

    return code, otp


# ──────────────────────────────────────────────────────────────
# STEP 2: Import vào pool
# ──────────────────────────────────────────────────────────────
def step_2_import():
    sep("STEP 2 — Import email vào pool")

    # Xóa nếu đã tồn tại từ run trước
    db.delete_gmail_api_url_email(EMAIL)

    inserted, skipped = db.import_gmail_api_url_emails(
        [{"email": EMAIL, "code_url": CODE_URL}]
    )
    print(f"Inserted : {inserted}")
    print(f"Skipped  : {skipped}")

    row = db.get_gmail_api_url_email_by_email(EMAIL)
    print(f"\nDB row:")
    print(f"  email   = {row['email']}")
    print(f"  code_url = {row['code_url'][:60]}...")
    print(f"  status  = {row['status']}")

    assert inserted == 1
    assert row["status"] == "available"
    print("\n✅ Import OK")
    return row


# ──────────────────────────────────────────────────────────────
# STEP 3: Claim từ pool
# ──────────────────────────────────────────────────────────────
def step_3_claim():
    sep("STEP 3 — Claim email từ pool")

    account = pick_account()
    print(f"email    = {account.email}")
    print(f"code_url = {account.code_url[:60]}...")

    row = db.get_gmail_api_url_email_by_email(account.email)
    print(f"DB status = {row['status']}")

    assert account.email == EMAIL
    assert row["status"] == "used"
    print("\n✅ Claim OK — status đã chuyển sang 'used'")
    return account


# ──────────────────────────────────────────────────────────────
# STEP 4: Poll THẬT — gọi real URL qua client code
# ──────────────────────────────────────────────────────────────
def step_4_real_poll(account: GmailApiUrlAccount, api_code: int):
    sep("STEP 4 — Poll THẬT qua gmail_api_url_client")

    if api_code == 602:
        print("⚠️  API đang trả 602 — test exception path")
        try:
            poll_verification_code(account, max_wait=5, poll_interval=1)
            print("❌ Phải raise GmailApiUrlError nhưng không raise!")
            return None
        except GmailApiUrlError as e:
            print(f"✅ GmailApiUrlError raised đúng: {e}")
            return "error_602"

    elif api_code == 601:
        print("⚠️  API đang trả 601 — test timeout path (max_wait=5s)")
        try:
            otp = poll_verification_code(account, max_wait=5, poll_interval=2)
            print(f"✅ Có OTP (API chuyển sang trả 0 trong lúc poll): {otp}")
            return otp
        except GmailApiUrlError as e:
            print(f"✅ Timeout đúng hành vi: {e}")
            return "timeout_601"

    else:  # code == 0
        print("API đang trả SUCCESS — poll sẽ lấy OTP ngay lần đầu")
        try:
            otp = poll_verification_code(account, max_wait=30, poll_interval=2)
            print(f"\n✅ poll_verification_code trả OTP = {otp}")
            return otp
        except GmailApiUrlError as e:
            print(f"❌ Không ngờ raise exception: {e}")
            return None


# ──────────────────────────────────────────────────────────────
# STEP 5: email_provider.resolve_email_source
# ──────────────────────────────────────────────────────────────
def step_5_resolve_source():
    sep("STEP 5 — email_provider.resolve_email_source")

    source = email_provider.resolve_email_source(EMAIL)
    print(f"resolve_email_source('{EMAIL}') = '{source}'")

    assert source == "gmail_api_url", f"Expected 'gmail_api_url', got '{source}'"
    print("✅ Source nhận diện đúng")


# ──────────────────────────────────────────────────────────────
# STEP 6: Release và verify status cuối
# ──────────────────────────────────────────────────────────────
def step_6_release(api_code: int):
    sep("STEP 6 — Release và kiểm tra status cuối")

    if api_code == 602:
        # 602 → đánh dấu failed để track refund
        release_account(EMAIL, status="failed", note="code=602 từ provider, yêu cầu refund")
        expected_status = "failed"
        print("Note: code=602 nên status = 'failed' (để track refund)")
    else:
        # Thành công hoặc timeout → trả về available
        release_account(EMAIL, status="available", note="test run completed")
        expected_status = "available"

    row = db.get_gmail_api_url_email_by_email(EMAIL)
    print(f"DB status = {row['status']}")
    print(f"DB note   = {row.get('note', '')}")

    assert row["status"] == expected_status
    print(f"\n✅ Status '{expected_status}' đúng")


# ──────────────────────────────────────────────────────────────
# STEP 7: Cleanup
# ──────────────────────────────────────────────────────────────
def step_7_cleanup():
    sep("STEP 7 — Cleanup")
    deleted = db.delete_gmail_api_url_email(EMAIL)
    print(f"Deleted: {deleted}")
    assert deleted is True
    print("✅ Cleanup OK")


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────
def main():
    print("\n" + "=" * 60)
    print("  REAL GMAIL API URL — END-TO-END POLL TEST")
    print("  (KHÔNG chạy registration — chỉ test polling layer)")
    print("=" * 60)
    print(f"\nEmail   : {EMAIL}")
    print(f"API URL : {CODE_URL[:55]}...")

    results = []

    try:
        # Step 1: raw HTTP check
        api_code, api_otp = step_1_raw_api()
        results.append(("Raw API call", True))

        # Step 2-3: pool import/claim
        step_2_import()
        results.append(("Import to pool", True))

        account = step_3_claim()
        results.append(("Claim from pool", True))

        # Step 4: real poll via client
        otp_result = step_4_real_poll(account, api_code)
        results.append(("Real poll via client", otp_result is not None))

        # Step 5: provider integration
        step_5_resolve_source()
        results.append(("resolve_email_source", True))

    except AssertionError as e:
        print(f"\n❌ ASSERTION FAILED: {e}")
        results.append(("Assertion error", False))
    except Exception as e:
        print(f"\n❌ UNEXPECTED ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Unexpected error", False))
    finally:
        # Luôn release và cleanup dù pass hay fail
        try:
            step_6_release(api_code if 'api_code' in dir() else 0)
            results.append(("Release", True))
        except Exception as e:
            results.append(("Release", False))
        try:
            step_7_cleanup()
            results.append(("Cleanup", True))
        except Exception as e:
            results.append(("Cleanup", False))

    # Final report
    sep("FINAL RESULTS")
    passed = sum(1 for _, r in results if r)
    for name, ok in results:
        print(f"{'✅' if ok else '❌'} {name}")

    print(f"\n{'='*60}")
    print(f"  {passed}/{len(results)} steps passed")
    print(f"{'='*60}\n")

    # Nếu API trả code=0 thì in OTP thật (chỉ dùng để xác nhận, không đăng ký)
    if 'api_otp' in dir() and api_otp:
        print(f"⚠️  OTP hiện tại từ provider: {api_otp}")
        print("   (OTP này dùng cho email đã được gửi từ trước, không phải từ bước đăng ký mới)")

    return passed == len(results)


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
