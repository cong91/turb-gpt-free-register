import unittest

from core.paymesh_aliases import (
    PaymeshAliasError,
    alias_suffix,
    build_paymesh_alias_plan,
    normalize_paymesh_routed_domains,
)


class PaymeshAliasesNormalizeTests(unittest.TestCase):
    def test_accepts_test_and_invalid_tlds(self):
        self.assertEqual(
            normalize_paymesh_routed_domains(["test.com", "mail.invalid"]),
            ("test.com", "mail.invalid"),
        )

    def test_accepts_localhost(self):
        self.assertEqual(normalize_paymesh_routed_domains(["localhost"]), ("localhost",))

    def test_dedup_and_strip_and_lowercase(self):
        self.assertEqual(
            normalize_paymesh_routed_domains([" Test.COM ", "test.com", "MAIL.test"]),
            ("test.com", "mail.test"),
        )

    def test_rejects_more_than_two(self):
        with self.assertRaisesRegex(PaymeshAliasError, "tối đa"):
            normalize_paymesh_routed_domains(["a.test", "b.test", "c.test"])

    def test_rejects_ip(self):
        with self.assertRaisesRegex(PaymeshAliasError, "IP"):
            normalize_paymesh_routed_domains(["127.0.0.1"])

    def test_rejects_collides_with_source_domain(self):
        with self.assertRaisesRegex(PaymeshAliasError, "trùng"):
            normalize_paymesh_routed_domains(["gmail.com"], source_domain="gmail.com")

    def test_rejects_string_input(self):
        with self.assertRaises(PaymeshAliasError):
            normalize_paymesh_routed_domains("test.com")  # type: ignore[arg-type]

    def test_rejects_invalid_label(self):
        with self.assertRaises(PaymeshAliasError):
            normalize_paymesh_routed_domains(["bad domain.com"])


class PaymeshAliasesPlanTests(unittest.TestCase):
    def test_plan_original_block_matches_alias_variants_shape(self):
        from core.paymesh_mail_client import _alias_variants

        email = "user@gmail.com"
        plan = build_paymesh_alias_plan(email, limit=6)
        original = [c.email for c in plan.original_candidates]
        self.assertEqual(original, _alias_variants(email, 6))
        self.assertTrue(all(c.phase == "original" for c in plan.original_candidates))
        self.assertEqual(plan.routed_candidates, ())

    def test_plan_with_one_routed_domain_doubles_candidates(self):
        plan = build_paymesh_alias_plan("user@gmail.com", limit=6, routed_domains=["test.com"])
        self.assertEqual(len(plan.original_candidates), 6)
        self.assertEqual(len(plan.routed_candidates), 6)
        routed_emails = [c.email for c in plan.routed_candidates]
        self.assertTrue(all(email.endswith("@test.com") for email in routed_emails))
        original_emails = [c.email for c in plan.original_candidates]
        self.assertEqual(len(set(original_emails + routed_emails)), 12)

    def test_plan_with_two_routed_domains_triples_candidates(self):
        plan = build_paymesh_alias_plan(
            "user@gmail.com", limit=4, routed_domains=["test.com", "mail.invalid"]
        )
        self.assertEqual(len(plan.original_candidates), 4)
        self.assertEqual(len(plan.routed_candidates), 8)
        domains = {c.domain for c in plan.routed_candidates}
        self.assertEqual(domains, {"test.com", "mail.invalid"})

    def test_alias_suffix_stable(self):
        self.assertEqual(alias_suffix("user@gmail.com", 0), alias_suffix("user@gmail.com", 0))
        self.assertEqual(len(alias_suffix("user@gmail.com", 0)), 5)


if __name__ == "__main__":
    unittest.main()