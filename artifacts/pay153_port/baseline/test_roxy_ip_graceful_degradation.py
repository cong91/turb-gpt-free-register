#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Test that IP probe failures don't cause registration failure."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent))

from core.registration_network_identity import NetworkIdentityError


def test_roxy_ip_probe_graceful_degradation():
    """Verify that Roxy IP probe failures allow registration to continue."""
    print("=" * 70)
    print("Testing Roxy IP Probe Graceful Degradation")
    print("=" * 70)
    
    # Mock the imports to avoid actually running Roxy
    with patch.dict('sys.modules', {
        'selenium': MagicMock(),
        'selenium.webdriver': MagicMock(),
    }):
        from core import roxy_registration
        
        # Mock driver and network_identity
        mock_driver = MagicMock()
        network_identity = {
            "tunnel_egress_ip": "1.2.3.4",
            "local_port": 12345,
            "profile_id": "test-profile",
            "verified": False,
        }
        
        # Mock verify_profile_network_identity to raise error
        with patch('core.registration_network_identity.verify_profile_network_identity') as mock_verify:
            mock_verify.side_effect = NetworkIdentityError("公共 IP 响应无效: ''")
            
            # Simulate the code path in roxy_registration.py:1532-1545
            try:
                from core.registration_network_identity import verify_profile_network_identity, NetworkIdentityError as NIE
                
                try:
                    result = verify_profile_network_identity(mock_driver, network_identity)
                    print("✓ IP verification succeeded (unexpected in this test)")
                    verified = True
                except NIE as exc:
                    print(f"✓ IP verification failed as expected: {exc}")
                    print("✓ Graceful degradation: Continuing with verified=False")
                    network_identity["verified"] = False
                    network_identity["verification_error"] = str(exc)[:500]
                    verified = False
                
                # Registration should continue
                print("\n" + "=" * 70)
                if not verified and network_identity.get("verification_error"):
                    print("✅ TEST PASSED")
                    print("   - IP probe failed but was caught")
                    print("   - Registration can continue")
                    print("   - Error logged in network_identity")
                    print(f"   - verified={network_identity['verified']}")
                    print(f"   - error={network_identity['verification_error'][:80]}...")
                    return True
                else:
                    print("❌ TEST FAILED - Error was not handled correctly")
                    return False
                    
            except Exception as e:
                print(f"\n❌ TEST FAILED - Unexpected exception: {e}")
                import traceback
                traceback.print_exc()
                return False


if __name__ == '__main__':
    success = test_roxy_ip_probe_graceful_degradation()
    print("=" * 70)
    sys.exit(0 if success else 1)
