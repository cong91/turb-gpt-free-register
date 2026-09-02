import tempfile
import unittest
from pathlib import Path

from core.hero_sms_country_store import HeroSmsCountryStore, make_profile_key


class HeroSmsCountryStoreTests(unittest.TestCase):
    def test_single_failure_is_persisted_without_immediate_block(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            store = HeroSmsCountryStore(state_path)
            profile = make_profile_key("https://hero.test/api", "dr", "")

            store.mark_unusable(profile, "52", "phone verification rejected")
            self.assertEqual(store.blocked_countries(profile), set())

            reopened = HeroSmsCountryStore(state_path)
            self.assertEqual(reopened.blocked_countries(profile), set())

    def test_successful_country_keeps_price(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            store = HeroSmsCountryStore(state_path)
            profile = make_profile_key("https://hero.test/api", "dr", "")

            self.assertTrue(store.mark_verified(profile, "16", "0.08"))
            reopened = HeroSmsCountryStore(state_path)
            self.assertEqual(reopened.verified_countries(profile), {"16": "0.08"})

    def test_high_failure_rate_blocks_only_after_minimum_samples(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HeroSmsCountryStore(Path(temp_dir) / "state.sqlite3")
            profile = make_profile_key("https://hero.test/api", "dr", "")

            for _ in range(3):
                store.mark_unusable(profile, "52", "otp timeout")
            self.assertEqual(store.blocked_countries(profile), set())

            store.mark_unusable(profile, "52", "otp timeout")
            self.assertEqual(store.blocked_countries(profile), {"52"})

    def test_successes_can_recover_a_high_failure_rate_country(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HeroSmsCountryStore(Path(temp_dir) / "state.sqlite3")
            profile = make_profile_key("https://hero.test/api", "dr", "")

            for _ in range(4):
                store.mark_unusable(profile, "52", "otp timeout")
            self.assertEqual(store.blocked_countries(profile), {"52"})

            self.assertTrue(store.mark_verified(profile, "52", "0.03"))
            self.assertTrue(store.mark_verified(profile, "52", "0.03"))
            self.assertEqual(store.blocked_countries(profile), set())
            self.assertEqual(store.verified_countries(profile), {"52": "0.03"})

    def test_provider_health_aggregates_outcomes_across_price_profiles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HeroSmsCountryStore(Path(temp_dir) / "state.sqlite3")
            low_price = make_profile_key("https://hero.test/api", "dr", "0.05")
            higher_price = make_profile_key("https://hero.test/api", "dr", "0.1")

            for _ in range(4):
                store.mark_unusable(low_price, "31", "otp timeout")
            store.mark_verified(higher_price, "44", "0.075")

            self.assertEqual(store.blocked_countries_for_provider("https://hero.test/api", "dr"), {"31"})
            self.assertEqual(
                store.verified_countries_for_provider("https://hero.test/api", "dr"),
                {"44": "0.075"},
            )

    def test_sticky_countries_are_scoped_to_the_lane_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HeroSmsCountryStore(Path(temp_dir) / "state.sqlite3")
            lane_a = make_profile_key("https://hero.test/api", "dr", "", lane_key="worker-a")
            lane_b = make_profile_key("https://hero.test/api", "dr", "", lane_key="worker-b")

            store.mark_verified(lane_a, "52", "0.03")
            store.mark_verified(lane_b, "16", "0.08")

            self.assertEqual(store.sticky_countries(lane_a), ["52"])
            self.assertEqual(store.sticky_countries(lane_b), ["16"])

    def test_repeated_phone_used_failures_block_country_at_dedicated_threshold(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HeroSmsCountryStore(Path(temp_dir) / "state.sqlite3")
            profile = make_profile_key("https://hero.test/api", "dr", "")

            for _ in range(2):
                store.mark_number_rejected(profile, "52", "phone_used_or_max")
            self.assertEqual(store.blocked_countries(profile), set())

            store.mark_number_rejected(profile, "52", "phone_used_or_max")
            self.assertEqual(store.blocked_countries(profile), {"52"})

    def test_success_resets_dedicated_phone_rejection_counter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HeroSmsCountryStore(Path(temp_dir) / "state.sqlite3")
            profile = make_profile_key("https://hero.test/api", "dr", "")

            for _ in range(3):
                store.mark_number_rejected(profile, "52", "phone_used_or_max")
            store.mark_verified(profile, "52", "0.03")

            self.assertEqual(store.blocked_countries(profile), set())
            self.assertEqual(store.country_health(profile)["52"]["number_rejected_count"], 0)


if __name__ == "__main__":
    unittest.main()
