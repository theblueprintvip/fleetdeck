import unittest

import server

SAMPLE = ("FREELANCE SERVICES AGREEMENT. 1. License. Contractor grants Company a perpetual, irrevocable license to all deliverables. "
          "2. Term. This Agreement shall automatically renew for successive one-year terms unless cancelled 90 days before expiry. "
          "3. Indemnity. Contractor shall indemnify, defend and hold harmless Company from any and all claims, losses and expenses. "
          "4. Changes. Company may modify these terms at any time without notice. "
          "5. Delays. If Contractor misses a deadline, Contractor shall pay liquidated damages of $500 per day. "
          "6. Disputes. The courts of Delaware have exclusive jurisdiction over any dispute. 7. Payment. Company pays invoices within 60 days.")
CLEAN = ("SERVICES AGREEMENT. 1. Fees. Invoices are due within 14 days. 2. Term. This Agreement lasts 12 months and renews only by "
         "written agreement of both parties. 3. Ending. Either party may end this Agreement with 30 days' written notice.")


class Engine(unittest.TestCase):
    def test_same_scores_as_web_app(self):
        self.assertEqual(server.analyze_contract(SAMPLE).risk, 87)
        self.assertEqual(server.analyze_contract(CLEAN).risk, 12)

    def test_side_drops_clauses_that_favour_you(self):
        text = SAMPLE + " Contractor shall not compete with Company for two years."
        neutral = {t.id for t in server.analyze_contract(text).traps}
        buyer = {t.id for t in server.analyze_contract(text, side="buyer").traps}
        self.assertIn("perp", neutral)
        self.assertIn("noncomp", neutral)
        self.assertNotIn("perp", buyer)
        self.assertNotIn("noncomp", buyer)
        self.assertIn("auto", buyer)

    def test_aggressive_buyer_redline(self):
        r = server.redline_contract(SAMPLE, side="buyer", tone="aggressive")
        self.assertTrue(all(c.stance == "Required change" for c in r.changes))
        self.assertIn("Customer", r.clean_text)
        self.assertIn("[DELETE:", r.redlined_text)
        self.assertLess(r.risk_after, r.risk_before)

    def test_balanced_redline_cleans_to_base(self):
        r = server.redline_contract(SAMPLE)
        self.assertEqual(r.risk_after, 12)

    def test_every_rewrite_scans_clean(self):
        for t in server.TRAPS:
            self.assertEqual(server.analyze_contract(t["fix"]).traps, [], t["id"])
            for side, (us, them) in server.PARTIES.items():
                strong = server.STRONG[t["id"]].format(us=us, them=them)
                self.assertEqual(server.analyze_contract(strong, side=side).traps, [], f"{t['id']} {side}")

    def test_aggressive_buyer_redline_cleans_to_base(self):
        r = server.redline_contract(SAMPLE, side="buyer", tone="aggressive")
        self.assertEqual(r.risk_after, 12)

    def test_quote_keeps_first_letter(self):
        t = server.analyze_contract("Licensor grants a perpetual licence. Payment is due in 30 days.").traps[0]
        self.assertEqual(t.quote, "Licensor grants a perpetual licence.")

    def test_clause_lookup(self):
        self.assertEqual(server.get_clause("Give me a limitation of liability clause").clause, "limitation of liability")
        self.assertEqual(server.get_clause("lol", side="buyer").clause, "limitation of liability")
        self.assertEqual(server.get_clause("non-compete").clause, "non-solicitation")
        with self.assertRaises(ValueError):
            server.get_clause("pizza")

    def test_library_clauses_are_trap_free_when_neutral(self):
        for key, c in server.CLAUSES.items():
            self.assertEqual(server.analyze_contract(c["neutral"]).traps, [], key)

    def test_compare(self):
        v2 = server.redline_contract(SAMPLE).clean_text
        c = server.compare_versions(SAMPLE, v2)
        self.assertEqual((c.risk_v1, c.risk_v2), (87, 12))
        self.assertEqual(len(c.fixed), 6)
        self.assertEqual(c.introduced, [])
        self.assertTrue(c.verdict.startswith("v2 is safer"))

    def test_country_note(self):
        self.assertTrue(server.country_note("Germany").startswith("Germany: civil-law"))
        self.assertTrue(server.country_note("UK").startswith("United Kingdom: common-law"))
        self.assertEqual(len(server.KB["countries"]), 195)
        with self.assertRaises(ValueError):
            server.country_note("Atlantis")


if __name__ == "__main__":
    unittest.main()
