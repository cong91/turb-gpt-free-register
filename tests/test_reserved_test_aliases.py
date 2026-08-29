# -*- coding: utf-8 -*-
import unittest

from core.reserved_test_aliases import (
    ReservedTestAliasError,
    generate_reserved_test_aliases,
)


class ReservedTestAliasTests(unittest.TestCase):
    def test_accepts_one_or_two_reserved_test_domains(self):
        one_domain = generate_reserved_test_aliases("abcdef", ["mail.test"], limit=6)
        two_domains = generate_reserved_test_aliases(
            "abcdef",
            ["mail.test", "inbox.invalid"],
            limit=6,
        )

        self.assertEqual(len(one_domain), 6)
        self.assertEqual(len(two_domains), 6)
        self.assertTrue(all(address.endswith("@mail.test") for address in one_domain))
        self.assertEqual(
            two_domains[:2],
            ["abcdef@mail.test", "abcdef@inbox.invalid"],
        )

    def test_normalizes_and_deduplicates_domains(self):
        variants = generate_reserved_test_aliases(
            "abcdef",
            [" Mail.TEST. ", "mail.test", "Inbox.Example"],
            limit=6,
        )

        self.assertEqual(variants[:2], ["abcdef@mail.test", "abcdef@inbox.example"])
        self.assertEqual(len(variants), 6)

    def test_generation_is_deterministic_unique_and_bounded(self):
        first = generate_reserved_test_aliases(
            "a",
            ["mail.test", "inbox.invalid"],
            limit=200,
        )
        second = generate_reserved_test_aliases(
            "a",
            ["mail.test", "inbox.invalid"],
            limit=200,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 200)
        self.assertEqual(len(set(first)), 200)
        self.assertTrue(any("+" in address.split("@", 1)[0] for address in first))

    def test_rejects_missing_or_more_than_two_domains(self):
        with self.assertRaises(ReservedTestAliasError):
            generate_reserved_test_aliases("abcdef", [], limit=6)
        with self.assertRaises(ReservedTestAliasError):
            generate_reserved_test_aliases(
                "abcdef",
                ["one.test", "two.invalid", "three.example"],
                limit=6,
            )
        with self.assertRaises(ReservedTestAliasError):
            generate_reserved_test_aliases("abcdef", "one.test", limit=6)

    def test_rejects_real_or_malformed_domains(self):
        for domain in (
            "gmail.com",
            "googlemail.com",
            "example.org",
            "localhost",
            "mail.test/path",
        ):
            with self.subTest(domain=domain):
                with self.assertRaises(ReservedTestAliasError):
                    generate_reserved_test_aliases("abcdef", [domain], limit=6)

    def test_rejects_invalid_base_and_count(self):
        for base in ("", "has.dot", "has+tag", "a" * 33):
            with self.subTest(base=base):
                with self.assertRaises(ReservedTestAliasError):
                    generate_reserved_test_aliases(base, ["mail.test"], limit=6)
        for limit in (0, 1001, "invalid"):
            with self.subTest(limit=limit):
                with self.assertRaises(ReservedTestAliasError):
                    generate_reserved_test_aliases("abcdef", ["mail.test"], limit=limit)


if __name__ == "__main__":
    unittest.main()
