import base64
import json
import unittest

from steamsorter.steam import parse_release, rating_score, steamid_from_token, token_expiry


def make_token(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


class SteamHelpers(unittest.TestCase):
    def test_parse_release(self):
        self.assertEqual(parse_release("18 Apr, 2011"), "2011-04-18")
        self.assertEqual(parse_release("Apr 18, 2011"), "2011-04-18")
        self.assertEqual(parse_release("Q3 2025"), "2025-00-00")
        self.assertEqual(parse_release("Coming soon"), "")

    def test_rating_score(self):
        self.assertIsNone(rating_score(0, 0))
        self.assertAlmostEqual(rating_score(990, 10), 0.929, places=3)
        # few reviews get pulled towards 50%
        self.assertLess(rating_score(5, 0), rating_score(5000, 0))

    def test_token(self):
        tok = make_token({"sub": "76561198000000001", "exp": 123})
        self.assertEqual(steamid_from_token(tok), "76561198000000001")
        self.assertEqual(token_expiry(tok), 123)
        self.assertIsNone(steamid_from_token("garbage"))


if __name__ == "__main__":
    unittest.main()
