import unittest

from core.gmail_aliases import (
    GmailAliasError,
    build_gmail_alias_plan,
    generate_gmail_dual_domain_aliases,
    generate_gmail_variants,
    normalize_routed_domains,
)


class GmailAliasTests(unittest.TestCase):
    def test_generates_original_two_random_dot_and_three_plus_variants(self):
        variants = generate_gmail_variants("abcdef@gmail.com")

        self.assertEqual(len(variants), 6)
        self.assertEqual(variants[0], "abcdef@gmail.com")
        self.assertTrue(all("." in email for email in variants[1:3]))
        self.assertTrue(all("+" in email for email in variants[3:]))
        self.assertEqual(len(set(variants)), 6)

    def test_dot_positions_change_between_generated_accounts(self):
        generated = [
            generate_gmail_variants(email, limit=2)[1]
            for email in (
                "haldirk517517@gmail.com",
                "charleskeith1351@gmail.com",
                "brookebudd8354@gmail.com",
            )
        ]
        positions = [address.split("@", 1)[0].index(".") for address in generated]

        self.assertGreater(len(set(positions)), 1)

    def test_short_local_part_fills_missing_dot_slots_with_plus_aliases(self):
        variants = generate_gmail_variants("a@gmail.com")

        self.assertEqual(variants[0], "a@gmail.com")
        self.assertEqual(len(variants), 6)
        self.assertTrue(all("+" in email for email in variants[1:]))

    def test_generation_canonicalizes_googlemail(self):
        first = generate_gmail_variants("A.B+old@googlemail.com")
        second = generate_gmail_variants("ab@gmail.com")

        self.assertEqual(first[0], "ab@gmail.com")
        self.assertEqual(second[0], "ab@gmail.com")
        self.assertEqual(len(first), len(second))
        self.assertEqual(len(set(first)), len(first))
        self.assertEqual(len(set(second)), len(second))

    def test_rejects_non_gmail_addresses_and_caps_limit(self):
        with self.assertRaises(GmailAliasError):
            generate_gmail_variants("user@example.com")

        self.assertEqual(len(generate_gmail_variants("abcdef@gmail.com", limit=99)), 6)
        self.assertEqual(generate_gmail_variants("abcdef@gmail.com", limit=0), [])

    def test_normalizes_and_deduplicates_routed_domains(self):
        self.assertEqual(
            normalize_routed_domains(
                [" Relay-One.NET. ", "relay-one.net", "Relay-Two.ORG"],
            ),
            ("relay-one.net", "relay-two.org"),
        )

    def test_rejects_reserved_or_malformed_routed_domains(self):
        for domain in ("relay.test", "relay.invalid", "relay.example", "https://relay.net", "relay.net/path", "*.relay.net", "localhost"):
            with self.subTest(domain=domain), self.assertRaises(GmailAliasError):
                normalize_routed_domains([domain])

        with self.assertRaises(GmailAliasError):
            normalize_routed_domains(["one.net", "two.net", "three.net"])

    def test_alias_plan_preserves_source_domain_and_uses_routed_blocks(self):
        plan = build_gmail_alias_plan(
            "A.B+old@googlemail.com",
            limit=3,
            routed_domains=["relay-one.net", "relay-two.org"],
        )

        self.assertEqual(plan.original_candidates[0].email, "ab@googlemail.com")
        self.assertEqual(len(plan.original_candidates), 3)
        self.assertEqual(len({candidate.email for candidate in plan.original_candidates}), 3)
        self.assertEqual(
            [candidate.domain for candidate in plan.routed_candidates],
            ["relay-one.net", "relay-one.net", "relay-two.org"],
        )
        self.assertEqual(
            [candidate.phase for candidate in plan.candidates],
            ["original"] * 3 + ["routed"] * 3,
        )
        self.assertEqual(len({candidate.email for candidate in plan.candidates}), 6)

    def test_alias_plan_without_routing_keeps_original_phase(self):
        plan = build_gmail_alias_plan("abcdef@gmail.com", limit=6)

        self.assertEqual([candidate.phase for candidate in plan.candidates], ["original"] * 6)
        self.assertEqual(plan.candidates[0].email, "abcdef@gmail.com")
        self.assertEqual(len({candidate.email for candidate in plan.candidates}), 6)

    def test_gmail_api_aliases_use_only_one_dotted_address(self):
        aliases = generate_gmail_dual_domain_aliases("abcdef@gmail.com", limit=12)

        dotted = [email for email in aliases if "." in email.split("@", 1)[0]]

        self.assertEqual(len(aliases), 12)
        self.assertNotIn("abcdef@gmail.com", aliases)
        self.assertLessEqual(len(dotted), 1)
        self.assertTrue(all("+" in email for email in aliases if email not in dotted))

    def test_gmail_api_aliases_do_not_reuse_dotted_source(self):
        source = "abcd.ef@gmail.com"
        aliases = generate_gmail_dual_domain_aliases(source, limit=12)

        self.assertNotIn(source, aliases)
        self.assertLessEqual(
            sum("." in email.split("@", 1)[0] for email in aliases),
            1,
        )


if __name__ == "__main__":
    unittest.main()
