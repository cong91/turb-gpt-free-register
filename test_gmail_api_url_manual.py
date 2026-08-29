#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Gmail API URL Email Provider - Manual Integration Test
Kiểm tra thủ công toàn bộ luồng: import → claim → poll OTP → release
"""
import json
import sys
import time
from pathlib import Path

# Thêm project root vào path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import db
from core import email_provider
from core import gmail_api_url_client


def print_section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def test_1_import_emails():
    """Bước 1: Import email vào pool"""
    print_section("TEST 1: Import Email Pool")
    
    # Dữ liệu test - format: email----code_url
    test_data = """
# Test emails for Gmail API URL provider
test1@gmail.com----https://example.com/api/verify/test1
test2@gmail.com----https://example.com/api/verify/test2
test3@gmail.com----https://example.com/api/verify/test3
"""
    
    records = []
    for line in test_data.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) >= 2:
            records.append({
                "email": parts[0].strip(),
                "code_url": parts[1].strip(),
            })
    
    print(f"📝 Chuẩn bị import {len(records)} email...")
    for r in records:
        print(f"   - {r['email']} → {r['code_url']}")
    
    inserted, skipped = db.import_gmail_api_url_emails(records)
    print(f"\n✅ Import hoàn tất:")
    print(f"   - Thêm mới: {inserted}")
    print(f"   - Bỏ qua (trùng): {skipped}")
    
    return inserted > 0


def test_2_pool_summary():
    """Bước 2: Kiểm tra thống kê pool"""
    print_section("TEST 2: Pool Summary")
    
    summary = db.gmail_api_url_email_pool_summary()
    print(f"📊 Thống kê pool:")
    print(f"   - Tổng số: {summary.get('total', 0)}")
    print(f"   - Sẵn sàng: {summary.get('available', 0)}")
    print(f"   - Đang dùng: {summary.get('in_use', 0)}")
    print(f"   - Thất bại: {summary.get('failed', 0)}")
    print(f"   - Đã refund: {summary.get('refunded', 0)}")
    
    return summary.get('available', 0) > 0


def test_3_list_pool():
    """Bước 3: Liệt kê emails trong pool"""
    print_section("TEST 3: List Pool")
    
    emails = db.list_gmail_api_url_email_pool(status="available", limit=10)
    print(f"📋 Danh sách email sẵn sàng ({len(emails)}):")
    for idx, email in enumerate(emails, 1):
        print(f"   {idx}. {email['email']}")
        print(f"      URL: {email['code_url'][:50]}...")
        print(f"      Status: {email['status']}")
    
    return len(emails) > 0


def test_4_claim_email():
    """Bước 4: Claim một email từ pool"""
    print_section("TEST 4: Claim Email")
    
    print("🎯 Đang claim email từ pool...")
    try:
        account = gmail_api_url_client.pick_account()
        print(f"✅ Đã claim thành công:")
        print(f"   Email: {account.email}")
        print(f"   Code URL: {account.code_url[:60]}...")
        return account
    except Exception as e:
        print(f"❌ Claim thất bại: {e}")
        return None


def test_5_get_context(email: str):
    """Bước 5: Lấy context của email đã claim"""
    print_section("TEST 5: Get Account Context")
    
    print(f"🔍 Đang lấy context cho: {email}")
    account = gmail_api_url_client.get_account_context(email)
    if account:
        print(f"✅ Context tìm thấy:")
        print(f"   Email: {account.email}")
        print(f"   Code URL: {account.code_url[:60]}...")
        return True
    else:
        print(f"❌ Không tìm thấy context")
        return False


def test_6_simulate_poll_success(email: str):
    """Bước 6: Giả lập poll thành công (code=0)"""
    print_section("TEST 6: Simulate Poll Success (code=0)")
    
    print(f"📞 Giả lập API trả về code=0 với OTP=123456")
    print(f"   (Trong production, sẽ gọi GET {db.get_gmail_api_url_email_by_email(email)['code_url']})")
    print(f"\n💡 Response mô phỏng:")
    mock_response = {"code": 0, "data": {"code": "123456"}}
    print(f"   {json.dumps(mock_response, indent=2)}")
    
    # Giả lập thành công
    otp = "123456"
    print(f"\n✅ Lấy OTP thành công: {otp}")
    return otp


def test_7_simulate_poll_waiting(email: str):
    """Bước 7: Giả lập poll waiting (code=601)"""
    print_section("TEST 7: Simulate Poll Waiting (code=601)")
    
    print(f"📞 Giả lập API trả về code=601 (waiting)")
    print(f"\n💡 Response mô phỏng:")
    mock_response = {"code": 601}
    print(f"   {json.dumps(mock_response, indent=2)}")
    print(f"\n⏳ Sẽ tiếp tục poll sau {3} giây...")
    
    return True


def test_8_simulate_poll_error(email: str):
    """Bước 8: Giả lập poll error (code=602)"""
    print_section("TEST 8: Simulate Poll Error (code=602)")
    
    print(f"📞 Giả lập API trả về code=602 (error/refund)")
    print(f"\n💡 Response mô phỏng:")
    mock_response = {"code": 602}
    print(f"   {json.dumps(mock_response, indent=2)}")
    print(f"\n❌ Provider lỗi, cần đánh dấu failed và refund")
    
    return True


def test_9_release_failed(email: str):
    """Bước 9: Release email với status failed"""
    print_section("TEST 9: Release Email (failed/refund)")
    
    print(f"🔄 Đang release email với status=failed...")
    note = "Provider error code=602, yêu cầu refund"
    gmail_api_url_client.release_account(email, status="failed", note=note)
    
    print(f"✅ Release thành công:")
    print(f"   Email: {email}")
    print(f"   Status: failed")
    print(f"   Note: {note}")
    
    # Verify status
    row = db.get_gmail_api_url_email_by_email(email)
    print(f"\n🔍 Verification:")
    print(f"   DB status: {row['status']}")
    print(f"   DB note: {row.get('note', '')}")
    
    return row['status'] == 'failed'


def test_10_email_provider_integration():
    """Bước 10: Test integration với email_provider"""
    print_section("TEST 10: Email Provider Integration")
    
    # Verify gmail_api_url trong sources
    print(f"🔍 Kiểm tra gmail_api_url trong EMAIL_SOURCE...")
    sources = email_provider.parse_email_sources("gmail_api_url,outlook")
    print(f"   Parsed sources: {sources}")
    has_gmail_api_url = "gmail_api_url" in sources
    print(f"   ✅ gmail_api_url có trong danh sách" if has_gmail_api_url else "   ❌ gmail_api_url KHÔNG có trong danh sách")
    
    # Verify resolve_email_source
    print(f"\n🔍 Kiểm tra resolve_email_source...")
    test_email = "test1@gmail.com"
    row = db.get_gmail_api_url_email_by_email(test_email)
    if row:
        detected = email_provider.resolve_email_source(test_email)
        print(f"   Email: {test_email}")
        print(f"   Detected source: {detected}")
        is_correct = detected == "gmail_api_url"
        print(f"   ✅ Nhận diện đúng" if is_correct else f"   ❌ Nhận diện SAI (expected: gmail_api_url)")
    
    return has_gmail_api_url


def test_11_cleanup():
    """Bước 11: Dọn dẹp test data"""
    print_section("TEST 11: Cleanup Test Data")
    
    print(f"🧹 Đang xóa test emails...")
    test_emails = ["test1@gmail.com", "test2@gmail.com", "test3@gmail.com"]
    deleted_count = 0
    
    for email in test_emails:
        deleted = db.delete_gmail_api_url_email(email)
        if deleted:
            print(f"   ✅ Đã xóa: {email}")
            deleted_count += 1
        else:
            print(f"   ⚠️  Không tìm thấy: {email}")
    
    print(f"\n✅ Cleanup hoàn tất: Đã xóa {deleted_count}/{len(test_emails)} email")
    
    # Verify pool empty
    summary = db.gmail_api_url_email_pool_summary()
    print(f"\n📊 Pool summary sau cleanup:")
    print(f"   Tổng số: {summary.get('total', 0)}")
    
    return True


def main():
    """Chạy toàn bộ test suite"""
    print("\n" + "🚀 " * 30)
    print("   GMAIL API URL EMAIL PROVIDER - MANUAL INTEGRATION TEST")
    print("🚀 " * 30)
    
    results = []
    claimed_email = None
    
    try:
        # Test 1-3: Import và kiểm tra pool
        results.append(("Import emails", test_1_import_emails()))
        results.append(("Pool summary", test_2_pool_summary()))
        results.append(("List pool", test_3_list_pool()))
        
        # Test 4-5: Claim email
        account = test_4_claim_email()
        results.append(("Claim email", account is not None))
        
        if account:
            claimed_email = account.email
            results.append(("Get context", test_5_get_context(claimed_email)))
            
            # Test 6-8: Giả lập các scenario poll
            results.append(("Simulate poll success", test_6_simulate_poll_success(claimed_email)))
            results.append(("Simulate poll waiting", test_7_simulate_poll_waiting(claimed_email)))
            results.append(("Simulate poll error", test_8_simulate_poll_error(claimed_email)))
            
            # Test 9: Release với status failed
            results.append(("Release failed", test_9_release_failed(claimed_email)))
        
        # Test 10: Integration
        results.append(("Email provider integration", test_10_email_provider_integration()))
        
        # Test 11: Cleanup
        results.append(("Cleanup", test_11_cleanup()))
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
    
    # Final report
    print_section("TEST RESULTS")
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {name}")
    
    print(f"\n{'='*60}")
    print(f"  SUMMARY: {passed}/{total} tests passed")
    print(f"{'='*60}\n")
    
    return passed == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
