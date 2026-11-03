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


class StoreItems(unittest.TestCase):
    ITEM = {
        "item_type": 0, "id": 1145360, "success": 1, "visible": True, "name": "Hades",
        "appid": 1145360,  # type omitted = game (protobuf default)
        "tagids": [19, 492],
        "tags": [{"tagid": 492, "weight": 900}, {"tagid": 19, "weight": 1200}, {"tagid": 99999, "weight": 5}],
        "categories": {"supported_player_categoryids": [2], "feature_categoryids": [22, 29], "controller_categoryids": [28]},
        "reviews": {"summary_filtered": {"review_count": 200000, "percent_positive": 98, "review_score": 9,
                                         "review_score_label": "Overwhelmingly Positive"}},
        "basic_info": {"short_description": "Defy the god of the dead &amp; hack and slash."},
        "release": {"steam_release_date": 1600387200},
        "assets": {"asset_url_format": "steam/apps/1145360/${FILENAME}?t=1", "header": "header.jpg",
                   "library_capsule": "abc/library_600x900.jpg", "library_hero": "library_hero.jpg"},
    }

    def test_parse(self):
        from steamsorter.steam import parse_store_item

        r = parse_store_item(self.ITEM, {19: "Action", 492: "Indie"})
        d, rv = r["details"], r["reviews"]
        self.assertEqual(d["type"], "game")
        self.assertEqual(d["genres"], ["Action", "Indie"])  # sorted by weight, unknown dropped
        self.assertEqual(d["categories"], ["Single-player", "Steam Achievements", "Full controller support"])
        self.assertEqual(d["release_sort"], "2020-09-18")
        self.assertIn("&", d["short_desc"])
        self.assertTrue(d["assets"]["capsule"].endswith("steam/apps/1145360/abc/library_600x900.jpg?t=1"))
        self.assertEqual(rv["positive"] + rv["negative"], 200000)
        self.assertEqual(rv["positive"], 196000)
        self.assertEqual(rv["review_desc"], "Overwhelmingly Positive")

    def test_dlc_and_empty(self):
        from steamsorter.steam import parse_store_item

        r = parse_store_item({"appid": 5, "type": 4}, {})
        self.assertEqual(r["details"]["type"], "dlc")
        self.assertEqual(r["reviews"], {"positive": 0, "negative": 0, "review_desc": ""})
        self.assertEqual(r["details"]["assets"]["capsule"], "")


class TokenPaste(unittest.TestCase):
    def test_extract(self):
        from steamsorter.steam import extract_token

        tok = make_token({"sub": "76561198000000001"})
        tok = tok.replace("header", "eyJhbGciOiJFZERTQSJ9")
        cases = [
            tok,
            f'"{tok}"',
            '{"success":1,"data":{"webapi_token":"%s"}}' % tok,
            # Firefox JSON viewer "copy all" format
            f'success\t1\ndata\t\nwebapi_token\t"{tok}"',
            f"JSON\nRaw Data\nHeaders\nSave Copy\nsuccess:\t1\ndata:\nwebapi_token:\t\"{tok}\"",
        ]
        for c in cases:
            self.assertEqual(extract_token(c), tok, c)


class StoreItemsResponse(unittest.TestCase):
    """Shape of a real IStoreBrowseService/GetItems response (id is a plain int)."""

    def test_store_items_real_shape(self):
        from unittest import mock

        from steamsorter.steam import SteamClient

        resp = {"response": {"store_items": [
            {"item_type": 0, "id": 620, "success": 1, "visible": True, "name": "Portal 2", "appid": 620,
             "reviews": {"summary_filtered": {"review_count": 100, "percent_positive": 99,
                                              "review_score_label": "Overwhelmingly Positive"}}},
            {"item_type": 0, "id": 999, "success": 2},  # unknown app: no appid key
            {"item_type": 0, "id": 400, "success": 1, "visible": True, "name": "Portal", "categories": "junk"},
        ]}}
        c = SteamClient()
        c.batch_throttle.interval = 0
        with mock.patch("steamsorter.steam.http_json", return_value=resp):
            out = c.store_items([620, 999, 400], {})
        self.assertEqual(out[620]["reviews"]["positive"], 99)
        self.assertIsNone(out[999])
        self.assertNotIn(400, out)  # malformed entry is skipped, not fatal

    def test_store_item_appid(self):
        from steamsorter.steam import store_item_appid

        self.assertEqual(store_item_appid({"id": 620}), 620)
        self.assertEqual(store_item_appid({"id": {"appid": 620}}), 620)
        self.assertEqual(store_item_appid({"appid": 70, "id": 70}), 70)
        self.assertEqual(store_item_appid({}), 0)
