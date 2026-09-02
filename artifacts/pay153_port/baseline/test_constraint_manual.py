#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Manual test for 1 API = 1 active job constraint."""
import tempfile
from pathlib import Path
from core.gmail_api_url_batch_store import GmailApiUrlBatchStore, GmailBatchConflict

def main():
    print("=== Testing 1 API = 1 Active Job Constraint ===\n")
    
    with tempfile.TemporaryDirectory() as tmp:
        store = GmailApiUrlBatchStore(Path(tmp) / 'test.db')
        
        # Simulate: 2 API emails, each with 2 aliases
        groups = [
            {
                'source_email': 'api1@gmail.com',
                'code_url': 'https://api1.mailsapi.com/get-code?uid=111',
                'aliases': ['api1+alias1@gmail.com', 'api1+alias2@gmail.com']
            },
            {
                'source_email': 'api2@gmail.com', 
                'code_url': 'https://api2.mailsapi.com/get-code?uid=222',
                'aliases': ['api2+alias1@gmail.com', 'api2+alias2@gmail.com']
            },
        ]
        
        batch_id = store.create_batch_multi(groups)
        print(f"✓ Created batch: {batch_id}\n")
        
        # Test 1: Claim from 2 different APIs (should succeed)
        print("Test 1: 2 workers, 2 APIs → Both should succeed")
        try:
            job1 = store.claim(batch_id, 'worker-1')
            print(f"  ✓ Worker 1 claimed: {job1.inventory_id.split('----')[0]}")
            
            job2 = store.claim(batch_id, 'worker-2')
            print(f"  ✓ Worker 2 claimed: {job2.inventory_id.split('----')[0]}")
        except GmailBatchConflict as e:
            print(f"  ✗ UNEXPECTED FAILURE: {e}")
            return False
        
        print()
        
        # Test 2: 3rd worker should be blocked
        print("Test 2: 3rd worker tries to claim → Should be BLOCKED")
        try:
            job3 = store.claim(batch_id, 'worker-3')
            print(f"  ✗ UNEXPECTED SUCCESS: {job3.inventory_id}")
            print("  ERROR: Constraint not working! 3rd worker should be blocked.")
            return False
        except GmailBatchConflict as e:
            print(f"  ✓ Correctly blocked: {e}")
        
        print()
        
        # Test 3: After worker 1 completes, worker 3 can proceed
        print("Test 3: Worker 1 completes → Worker 3 can now claim")
        store.complete(job1.assignment_id)
        print(f"  ✓ Worker 1 completed")
        
        try:
            job3 = store.claim(batch_id, 'worker-3')
            print(f"  ✓ Worker 3 claimed: {job3.inventory_id.split('----')[0]}")
        except GmailBatchConflict as e:
            print(f"  ✗ UNEXPECTED FAILURE: {e}")
            return False
        
        print()
        print("=" * 50)
        print("✓✓✓ ALL TESTS PASSED ✓✓✓")
        print("Constraint working correctly: 1 API = 1 active job at a time")
        print("=" * 50)
        return True

if __name__ == '__main__':
    success = main()
    exit(0 if success else 1)
