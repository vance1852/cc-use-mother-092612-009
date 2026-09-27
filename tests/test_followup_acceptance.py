import unittest

from night_market_foundation.followup_acceptance import run


class FollowupAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        statuses = result["final_statuses"]
        self.assertEqual("delivered", statuses["wellness"])
        self.assertEqual("skipped", statuses["skipped"])
        self.assertEqual("completed", statuses["acknowledged"])
        self.assertEqual("completed", statuses["unconfirmed"])
        self.assertEqual("delivered", statuses["recheck"])
        self.assertEqual(1, result["ticks"]["at_deadline"]["escalated"])
        self.assertEqual(1, result["ticks"]["after_restart"]["due_sent"])


if __name__ == "__main__":
    unittest.main()
