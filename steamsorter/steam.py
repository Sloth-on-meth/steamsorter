"""Minimal Steam Web API / Store API client (stdlib only)."""
from __future__ import annotations

import base64
import html
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.steampowered.com"
STORE = "https://store.steampowered.com"
UA = "SteamSorter/0.2 (+https://github.com/sloth-on-meth/steamsorter)"


class SteamError(Exception):
    pass


class Throttle:
    """Ensures at least `interval` seconds between calls (thread-safe)."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, stop: threading.Event | None = None) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            if stop is not None:
                stop.wait(delay)
            else:
                time.sleep(delay)

    def backoff(self, seconds: float) -> None:
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


def http_json(url: str, params: dict | None = None, timeout: float = 30) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read(300).decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        hint = {401: " (token/key rejected - expired or wrong?)", 403: " (access denied - token/key wrong or expired?)",
                429: " (rate limited)"}.get(e.code, "")
        endpoint = url.split("?")[0].replace(API + "/", "").replace(STORE + "/", "")
        raise SteamError(f"HTTP {e.code}{hint} from {endpoint}" + (f": {body[:160]}" if body and "<" not in body[:1] else "")) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SteamError(f"Network error: {e}") from e
    try:
        return json.loads(data or b"null")
    except ValueError as e:
        raise SteamError(f"Bad JSON from {url.split('?')[0]}") from e


_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def extract_token(text: str) -> str:
    """Find the access token in whatever the user pasted (raw JSON, the
    Firefox JSON viewer's copy format, just the token, with quotes...)."""
    m = _JWT.search(text or "")
    return m.group(0) if m else (text or "").strip().strip('"')


def steamid_from_token(token: str) -> str | None:
    """The store 'webapi_token' is a JWT whose 'sub' claim is the SteamID64."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload))["sub"])
    except Exception:
        return None


def token_expiry(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def rating_score(positive: int | None, negative: int | None) -> float | None:
    """SteamDB-style rating: pulls low-review-count games towards 50%."""
    if positive is None or negative is None:
        return None
    total = positive + negative
    if total == 0:
        return None
    avg = positive / total
    return avg - (avg - 0.5) * 2 ** (-math.log10(total + 1))


_DATE_FORMATS = ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%b %Y", "%B %Y", "%Y")


def parse_release(s: str | None) -> str:
    """Turn store release strings into sortable 'YYYY-MM-DD' (or '' if unknown)."""
    if not s:
        return ""
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.search(r"(19|20)\d{2}", s)
    return f"{m.group(0)}-00-00" if m else ""


# Steam store category ids (as used by IStoreBrowseService) -> names.
CATEGORY_NAMES = {
    1: "Multi-player", 2: "Single-player", 9: "Co-op", 20: "MMO", 22: "Steam Achievements",
    18: "Partial Controller Support", 28: "Full controller support", 24: "Shared/Split Screen",
    27: "Cross-Platform Multiplayer", 36: "Online PvP", 37: "Shared/Split Screen PvP",
    38: "Online Co-op", 39: "Shared/Split Screen Co-op", 44: "Remote Play Together",
    47: "LAN PvP", 48: "LAN Co-op", 49: "PvP",
}
# EStoreAppType; 0 (game) is the protobuf default, so it is usually omitted.
STORE_APP_TYPES = {0: "game", 1: "demo", 2: "mod", 3: "video", 4: "dlc", 5: "music", 6: "video", 7: "video"}
ASSET_CDN = "https://shared.fastly.steamstatic.com/store_item_assets/"


def parse_store_item(it: dict, tag_names: dict[int, str]) -> dict:
    """Turn an IStoreBrowseService StoreItem into our details+reviews dicts."""
    t = it.get("type", 0)
    tags = sorted(it.get("tags") or [], key=lambda x: -int(x.get("weight", 0)))
    tag_ids = [int(x["tagid"]) for x in tags] or [int(x) for x in it.get("tagids") or []]
    tag_list = [tag_names[i] for i in tag_ids if i in tag_names][:8]
    cats = it.get("categories") or {}
    cat_ids = []
    for k in ("supported_player_categoryids", "feature_categoryids", "controller_categoryids"):
        cat_ids += cats.get(k) or []
    rel = it.get("release") or {}
    ts = rel.get("steam_release_date") or rel.get("original_release_date")
    release = datetime.fromtimestamp(int(ts), timezone.utc) if ts else None
    a = it.get("assets") or {}
    fmt = a.get("asset_url_format") or ""

    def asset(key):
        f = a.get(key)
        return ASSET_CDN + fmt.replace("${FILENAME}", f) if f and fmt else ""

    rv = ((it.get("reviews") or {}).get("summary_filtered")) or {}
    count = int(rv.get("review_count") or 0)
    pct = int(rv.get("percent_positive") or 0)
    pos = round(count * pct / 100)
    details = {
        "type": STORE_APP_TYPES.get(int(t), "other"),
        "genres": tag_list,
        "categories": [CATEGORY_NAMES[c] for c in dict.fromkeys(cat_ids) if c in CATEGORY_NAMES],
        "release": f"{release.day} {release:%b %Y}" if release else "",
        "release_sort": release.strftime("%Y-%m-%d") if release else "",
        "metacritic": None,
        "short_desc": html.unescape((it.get("basic_info") or {}).get("short_description") or ""),
        "header_image": asset("header"),
        "assets": {"capsule": asset("library_capsule"), "hero": asset("library_hero"), "logo": asset("library_logo") if a.get("library_logo") else ""},
    }
    reviews = {"positive": pos, "negative": count - pos, "review_desc": rv.get("review_score_label") or ""}
    return {"details": details, "reviews": reviews}


class SteamClient:
    def __init__(self, api_key: str = "", access_token: str = ""):
        self.api_key = api_key.strip()
        self.access_token = access_token.strip()
        # Store appdetails is limited to ~200 requests / 5 min.
        self.details_throttle = Throttle(1.6)
        self.reviews_throttle = Throttle(0.35)
        self.batch_throttle = Throttle(0.3)

    # ---- Web API -------------------------------------------------------
    def _auth(self, prefer_token: bool = False) -> dict:
        if self.access_token and (prefer_token or not self.api_key):
            return {"access_token": self.access_token}
        if self.api_key:
            return {"key": self.api_key}
        raise SteamError("No Steam Web API key or access token configured (see Settings).")

    def _api(self, path: str, params: dict, prefer_token: bool = False) -> dict:
        p = dict(self._auth(prefer_token))
        p.update(params)
        data = http_json(f"{API}/{path}", p)
        if not isinstance(data, dict):
            raise SteamError(f"Unexpected response from {path}")
        return data.get("response", data)

    def resolve_steamid(self, text: str) -> str:
        """Accepts a SteamID64, a vanity name, or a profile URL."""
        text = text.strip().rstrip("/")
        m = re.search(r"/profiles/(\d{17})", text)
        if m:
            return m.group(1)
        if re.fullmatch(r"\d{17}", text):
            return text
        m = re.search(r"/id/([^/?#]+)", text)
        vanity = m.group(1) if m else text
        if not vanity:
            raise SteamError("No SteamID configured.")
        r = self._api("ISteamUser/ResolveVanityURL/v1/", {"vanityurl": vanity})
        if r.get("success") != 1:
            raise SteamError(f"Could not resolve Steam profile '{vanity}'.")
        return str(r["steamid"])

    def owned_games(self, steamid: str) -> list[dict]:
        r = self._api(
            "IPlayerService/GetOwnedGames/v1/",
            {"steamid": steamid, "include_appinfo": 1, "include_played_free_games": 1},
        )
        games = r.get("games")
        if games is None:
            raise SteamError(
                "Steam returned no games. If you use an API key from another account, "
                "set your profile's 'Game details' to Public."
            )
        return [
            {
                "appid": int(g["appid"]),
                "name": g.get("name") or f"App {g['appid']}",
                "playtime": int(g.get("playtime_forever", 0)),
                "last_played": int(g.get("rtime_last_played", 0)),
            }
            for g in games
        ]

    def family_games(self, steamid: str) -> list[dict]:
        """Apps shared with you through Steam Family (needs access token)."""
        if not self.access_token:
            return []
        g = self._api(
            "IFamilyGroupsService/GetFamilyGroupForUser/v1/", {"steamid": steamid}, prefer_token=True
        )
        group = g.get("family_groupid")
        if g.get("is_not_member_of_any_group"):
            raise SteamError("Steam says this account isn't in a Steam Family.")
        if not group or str(group) == "0":
            raise SteamError(f"Steam returned no family group for this token ({json.dumps(g)[:120]}). "
                             "Get a fresh token while logged in as yourself.")
        r = self._api(
            "IFamilyGroupsService/GetSharedLibraryApps/v1/",
            {
                "family_groupid": group,
                "include_own": "true",
                "include_excluded": "false",
                "include_free": "false",
                "include_non_games": "false",
                "language": "english",
                "steamid": steamid,
            },
            prefer_token=True,
        )
        apps = r.get("apps")
        if apps is None:
            raise SteamError(f"Steam returned no shared apps ({json.dumps(r)[:120]}).")
        out = []
        for a in apps:
            if a.get("exclude_reason", 0):
                continue
            owners = [str(o) for o in a.get("owner_steamids", [])]
            if steamid in owners:
                continue  # already in our own library
            out.append(
                {
                    "appid": int(a["appid"]),
                    "name": a.get("name") or f"App {a['appid']}",
                    "owners": owners,
                    # Steam does not report reliable per-user playtime here,
                    # only when *you* last played it.
                    "playtime": 0,
                    "last_played": int(a.get("rt_last_played", 0)),
                }
            )
        return out

    def player_names(self, steamids: list[str]) -> dict[str, str]:
        names: dict[str, str] = {}
        for i in range(0, len(steamids), 100):
            chunk = steamids[i : i + 100]
            try:
                r = self._api("ISteamUser/GetPlayerSummaries/v2/", {"steamids": ",".join(chunk)})
            except SteamError:
                continue
            for p in r.get("players", []):
                names[str(p["steamid"])] = p.get("personaname", p["steamid"])
        return names

    # ---- Store browse API: 100 games per request ----------------------
    def tag_names(self) -> dict[int, str]:
        data = http_json(f"{STORE}/tagdata/populartags/english")
        return {int(t["tagid"]): t["name"] for t in data or [] if "tagid" in t}

    def store_items(self, appids: list[int], tag_names: dict[int, str]) -> dict[int, dict]:
        req = {
            "ids": [{"appid": a} for a in appids],
            "context": {"language": "english", "country_code": "US", "steam_realm": 1},
            "data_request": {
                "include_assets": True, "include_release": True, "include_reviews": True,
                "include_basic_info": True, "include_tag_count": 20,
            },
        }
        self.batch_throttle.wait()
        data = http_json(f"{API}/IStoreBrowseService/GetItems/v1/", {"input_json": json.dumps(req)})
        items = ((data or {}).get("response") or {}).get("store_items")
        if items is None:
            raise SteamError("Unexpected response from IStoreBrowseService/GetItems")
        out = {}
        for it in items:
            appid = int(it.get("appid") or it.get("id", {}).get("appid", 0) or 0)
            if not appid:
                continue
            if int(it.get("success", 1)) != 1 or not it.get("visible", True) and not it.get("name"):
                out[appid] = None  # delisted / not on the store
                continue
            out[appid] = parse_store_item(it, tag_names)
        return out

    # ---- Store API, one game at a time (slow fallback) ----------------
    def _store(self, path: str, params: dict, throttle: Throttle, stop: threading.Event | None) -> dict:
        for attempt in range(5):
            throttle.wait(stop)
            if stop is not None and stop.is_set():
                raise SteamError("stopped")
            try:
                return http_json(f"{STORE}/{path}", params)
            except SteamError as e:
                if "HTTP 429" in str(e) or "HTTP 403" in str(e):
                    throttle.backoff(30 * (attempt + 1))  # rate limited, cool down
                    continue
                if attempt >= 2:
                    raise
                throttle.backoff(3)
        raise SteamError("Rate limited by Steam store")

    def app_details(self, appid: int, stop: threading.Event | None = None) -> dict | None:
        data = self._store(
            "api/appdetails", {"appids": appid, "l": "english", "cc": "us"}, self.details_throttle, stop
        )
        entry = (data or {}).get(str(appid)) or {}
        if not entry.get("success"):
            return None
        d = entry.get("data") or {}
        release = (d.get("release_date") or {}).get("date", "")
        return {
            "type": d.get("type", ""),
            "genres": [x["description"] for x in d.get("genres", []) if "description" in x],
            "categories": [x["description"] for x in d.get("categories", []) if "description" in x],
            "release": release,
            "release_sort": parse_release(release),
            "metacritic": (d.get("metacritic") or {}).get("score"),
            "short_desc": d.get("short_description", ""),
            "header_image": d.get("header_image", ""),
        }

    def app_reviews(self, appid: int, stop: threading.Event | None = None) -> dict:
        data = self._store(
            f"appreviews/{appid}",
            {"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0, "filter": "all"},
            self.reviews_throttle,
            stop,
        )
        q = (data or {}).get("query_summary") or {}
        return {
            "positive": int(q.get("total_positive", 0)),
            "negative": int(q.get("total_negative", 0)),
            "review_desc": q.get("review_score_desc", ""),
        }
