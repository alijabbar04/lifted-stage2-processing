"""Regression tests for Stage 2 document placement policy."""

import unittest

from _load_app import load_app


app = load_app()


class TestEmploymentContractOverwritePolicy(unittest.TestCase):

    def test_canonical_type_is_an_overwrite_document(self):
        self.assertIn("Employment Contract", app.OVERWRITE_TYPES)

    def test_exact_ranked_and_dated_contracts_are_overwrite_documents(self):
        filenames = (
            "Employment Contract",
            "Employment Contract (01)",
            "Employment Contract (12)",
            "Employment Contract - (24-04-2025)",
            "Employment Contract - (24-04-2025) (03)",
        )
        for stem in filenames:
            with self.subTest(stem=stem):
                self.assertTrue(app.is_overwrite_type(stem))

    def test_contract_amendment_remains_a_distinct_bulk_type(self):
        self.assertFalse(app.is_overwrite_type("Employment Contract Amendment"))


if __name__ == "__main__":
    unittest.main()
