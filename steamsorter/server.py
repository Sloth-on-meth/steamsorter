"""Local HTTP backend: holds library state, fetches data, serves the web UI."""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .steam import SteamClient, SteamError, extract_token, rating_score, steamid_from_token, token_expiry
from .store import Cache, Config, Game, is_stale

WEB_DIR = Path(__file__).with_name("web")
BATCH = 100  # games per IStoreBrowseService request
IDLE_TIMEOUT = 120  # seconds without the page polling before we quit (browser-tab mode)


def open_url(url: str) -> None:
    import os
    import subprocess
    import sys
    import webbrowser

    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        webbrowser.open(url)


class Library:
    """Thread-safe library state shared by the HTTP handlers and fetch workers."""

    def __init__(self, client_factory=SteamClient):
        self.lock = threading.RLock()
        self.client_factory = client_factory
        self.cfg = Config.load()
        self.cache = Cache()
        self.names = self.cache.load_names()
        self.games: dict[int, Game] = {g.appid: g for g in self.cache.load_library()}
        self.rev = 1
        self.game_rev: dict[int, int] = {a: 1 for a in self.games}
        self.status = ""
        self.status_level = "info"
        self.loading = False
        self.stop = threading.Event()
        self.last_ping = time.monotonic()

    # ---- serialisation -------------------------------------------------
    def owner_names(self, g: Game) -> list[str]:
        return [self.names.get(o, "Family member") for o in g.owners]

    def game_json(self, g: Game) -> dict:
        score = rating_score(g.positive, g.negative)
        return {
            "appid": g.appid,
            "name": g.name,
            "source": g.source,
            "owners": self.owner_names(g) if g.source == "family" else [],
            "playtime": g.playtime,
            "last_played": g.last_played,
            "type": g.type,
            "genres": g.genres,
            "categories": g.categories,
            "release": g.release,
            "release_sort": g.release_sort,
            "metacritic": g.metacritic,
            "desc": g.short_desc,
            "header": g.header_image,
            "capsule": g.assets.get("capsule", ""),
            "hero": g.assets.get("hero", ""),
            "logo": g.assets.get("logo", ""),
            "positive": g.positive,
            "negative": g.negative,
            "reviews": g.total_reviews,
            "rating": g.rating,
            "score": score,
            "verdict": g.review_desc,
            "has_details": bool(g.details_ts),
            "has_reviews": bool(g.reviews_ts),
        }

    def progress(self) -> dict:
        total = len(self.games)
        return {
            "total": total,
            "reviews": sum(1 for g in self.games.values() if g.reviews_ts),
            "details": sum(1 for g in self.games.values() if g.details_ts),
        }

    def state(self, since: int = 0) -> dict:
        with self.lock:
            changed = [g for a, g in self.games.items() if self.game_rev.get(a, 0) > since]
            return {
                "rev": self.rev,
                "full": since == 0,
                "games": [self.game_json(g) for g in changed],
                "appids": list(self.games) if since else None,
                "progress": self.progress(),
                "status": self.status,
                "status_level": self.status_level,
                "loading": self.loading,
                "configured": bool(self.cfg.steam_id and (self.cfg.api_key or self.cfg.family_token)),
                "family": bool(self.cfg.family_token),
                "version": __version__,
            }

    def _touch(self, appid: int) -> None:
        self.rev += 1
        self.game_rev[appid] = self.rev

    def set_status(self, msg: str, level: str = "info") -> None:
        with self.lock:
            self.status, self.status_level = msg, level
            self.rev += 1

    # ---- settings --------------------------------------------------------
    def settings(self) -> dict:
        exp = token_expiry(self.cfg.family_token) if self.cfg.family_token else None
        return {**self.cfg.__dict__, "token_expires": exp}

    @staticmethod
    def make_config(data: dict) -> tuple[Config, str | None]:
        cfg = Config(**{k: str(data.get(k, "")).strip() for k in ("api_key", "steam_id", "family_token")})
        if cfg.family_token:
            cfg.family_token = extract_token(cfg.family_token)
            if not steamid_from_token(cfg.family_token):
                return cfg, ("That doesn't look like a Steam access token. Copy the whole "
                             "ajaxgetasyncconfig page (it should contain \"webapi_token\").")
            tok_sid = steamid_from_token(cfg.family_token)
            if not cfg.steam_id or cfg.steam_id == tok_sid:
                cfg.steam_id = tok_sid or ""
        if not (cfg.api_key or cfg.family_token):
            return cfg, "Enter an access token or an API key."
        if not cfg.steam_id:
            return cfg, "Enter your Steam ID or profile link."
        return cfg, None

    def test_settings(self, data: dict) -> list[dict]:
        """Run each step against Steam and report what happened (for the Settings dialog)."""
        cfg, err = self.make_config(data)
        if err:
            return [{"ok": False, "msg": err}]
        out = []
        client = self.client_factory(cfg.api_key, cfg.family_token)
        if cfg.family_token:
            exp = token_expiry(cfg.family_token)
            if exp and exp < time.time():
                out.append({"ok": False, "msg": "Access token has expired. Grab a fresh one."})
            else:
                hrs = f", valid for {max(0, int((exp - time.time()) // 3600))} more hours" if exp else ""
                out.append({"ok": True, "msg": f"Access token belongs to SteamID {steamid_from_token(cfg.family_token)}{hrs}."})
        try:
            sid = client.resolve_steamid(cfg.steam_id)
            out.append({"ok": True, "msg": f"Steam ID: {sid}"})
            n = len(client.owned_games(sid))
            out.append({"ok": True, "msg": f"Your library: {n:,} games."})
        except SteamError as e:
            out.append({"ok": False, "msg": f"Your library: {e}"})
            return out
        if cfg.family_token:
            try:
                fam = client.family_games(sid)
                owners = len({o for f in fam for o in f["owners"]})
                out.append({"ok": True, "msg": f"Steam Family: {len(fam):,} extra games shared by {owners} member(s)."})
            except SteamError as e:
                out.append({"ok": False, "msg": f"Steam Family: {e}"})
        else:
            out.append({"ok": False, "msg": "Steam Family: no access token set, so family games are skipped."})
        try:
            got = client.store_items([620], {})
            out.append({"ok": bool(got), "msg": "Store data (ratings, tags, art): fast batch API works." if got else "Store data: batch API returned nothing."})
        except SteamError as e:
            out.append({"ok": False, "msg": f"Store data batch API: {e} (will use slow fallback)."})
        return out

    def save_settings(self, data: dict) -> str | None:
        cfg, err = self.make_config(data)
        if err:
            return err
        with self.lock:
            self.cfg = cfg
            cfg.save()
        self.refresh()
        return None

    # ---- loading ---------------------------------------------------------
    def client(self) -> SteamClient:
        return self.client_factory(self.cfg.api_key, self.cfg.family_token)

    def refresh(self) -> None:
        with self.lock:
            if self.loading or not self.cfg.steam_id:
                return
            self.loading = True
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        cfg, client = self.cfg, self.client()
        self.set_status("Loading your library from Steam…")
        try:
            sid = client.resolve_steamid(cfg.steam_id)
            own = client.owned_games(sid)
            warn = ""
            fam: list[dict] = []
            if cfg.family_token:
                exp = token_expiry(cfg.family_token)
                if exp and exp < time.time():
                    warn = "Your Steam Family token expired. Paste a fresh one in Settings to see family games."
                else:
                    try:
                        fam = client.family_games(sid)
                    except SteamError as e:
                        warn = f"Couldn't load Steam Family games ({e}). The token may have expired."
            owners = sorted({o for f in fam for o in f["owners"]})
            names = client.player_names(owners) if owners else {}
        except SteamError as e:
            with self.lock:
                self.loading = False
            self.set_status(str(e), "error")
            return
        except Exception as e:  # pragma: no cover - never die silently
            with self.lock:
                self.loading = False
            self.set_status(f"{type(e).__name__}: {e}", "error")
            return

        new: list[Game] = []
        seen = set()
        for src, items in (("own", own), ("family", fam)):  # own wins over family
            for it in items:
                if it["appid"] in seen:
                    continue
                seen.add(it["appid"])
                new.append(Game(appid=it["appid"], name=it["name"], source=src,
                                owners=it.get("owners", []), playtime=it.get("playtime", 0),
                                last_played=it.get("last_played", 0)))
        with self.lock:
            self.names.update(names)
            self.cache.save_names(names)
            self.cache.save_library(new)
            self.games = {g.appid: g for g in self.cache.load_library()}
            self.rev += 1
            self.game_rev = {a: self.rev for a in self.games}
            self.loading = False
        if warn:
            self.set_status(warn, "warn")
        else:
            self.set_status(f"Library updated at {datetime.now():%H:%M}.")
        self.start_enrichment()

    def start_enrichment(self) -> None:
        self.stop.set()
        self.stop = threading.Event()
        with self.lock:
            gs = sorted(self.games.values(), key=lambda g: (-g.playtime, -g.last_played, g.name.lower()))
        # Unknown games first (most played first), then stale ones. Games cached by
        # older versions (no cover-art info) are refreshed too - it's cheap now.
        fresh = [g.appid for g in gs if not g.details_ts or not g.reviews_ts or not g.assets]
        stale = [g.appid for g in gs if g.appid not in set(fresh) and (is_stale(g.details_ts) or is_stale(g.reviews_ts))]
        ids = fresh + stale
        if ids:
            threading.Thread(target=self._enrich_batched, args=(ids, self.client(), self.stop), daemon=True).start()

    def _apply_details(self, appid: int, d: dict | None) -> None:
        self.cache.save_details(appid, d)
        g = self.games.get(appid)
        if g:
            d = d or {}
            g.type = d.get("type", "")
            g.genres = d.get("genres", [])
            g.categories = d.get("categories", [])
            g.release = d.get("release", "")
            g.release_sort = d.get("release_sort", "")
            g.metacritic = d.get("metacritic")
            g.short_desc = d.get("short_desc", "")
            g.header_image = d.get("header_image", "")
            g.assets = d.get("assets") or {}
            g.details_ts = int(time.time())

    def _apply_reviews(self, appid: int, r: dict) -> None:
        self.cache.save_reviews(appid, r)
        g = self.games.get(appid)
        if g:
            g.positive, g.negative, g.review_desc = r["positive"], r["negative"], r["review_desc"]
            g.reviews_ts = int(time.time())

    def _enrich_batched(self, appids: list[int], client: SteamClient, stop: threading.Event) -> None:
        """Ratings, tags, release dates and cover art for 100 games per request."""
        with self.lock:
            tags = self.cache.load_tags()
        try:
            fetched = client.tag_names()
            if fetched:
                tags = fetched
                with self.lock:
                    self.cache.save_tags(tags)
        except SteamError:
            pass  # use cached tag names
        failures = 0
        i = 0
        while i < len(appids):
            if stop.is_set():
                return
            chunk = appids[i:i + BATCH]
            try:
                res = client.store_items(chunk, tags)
            except SteamError as e:
                failures += 1
                if failures >= 3:
                    self.set_status(f"Steam's fast batch API failed ({e}); falling back to the slow per-game lookup.", "warn")
                    self._start_slow(appids[i:], client, stop)
                    return
                stop.wait(3 * failures)
                continue  # retry the same batch
            failures = 0
            i += BATCH
            if stop.is_set():
                return
            with self.lock:
                for appid in chunk:
                    item = res.get(appid)
                    if item is None:  # not on the store (delisted, region locked...)
                        self._apply_details(appid, None)
                        self._apply_reviews(appid, {"positive": 0, "negative": 0, "review_desc": ""})
                    else:
                        self._apply_details(appid, item["details"])
                        self._apply_reviews(appid, item["reviews"])
                    self._touch(appid)
                self.cache.commit()

    def _start_slow(self, appids: list[int], client: SteamClient, stop: threading.Event) -> None:
        for kind, fetch in (("reviews", client.app_reviews), ("details", client.app_details)):
            threading.Thread(target=self._enrich, args=(kind, appids, fetch, stop), daemon=True).start()

    def _enrich(self, kind: str, appids: list[int], fetch, stop: threading.Event) -> None:
        for appid in appids:
            if stop.is_set():
                return
            try:
                data = fetch(appid, stop)
            except SteamError:
                continue  # retried on next launch
            if stop.is_set():
                return
            with self.lock:
                if kind == "reviews":
                    self._apply_reviews(appid, data)
                else:
                    self._apply_details(appid, data)
                self.cache.commit()
                self._touch(appid)

    def close(self) -> None:
        self.stop.set()
        with self.lock:
            self.cache.commit()
            self.cache.close()


def make_handler(lib: Library, token: str, port_ref: list):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SteamSorter"

        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _host_ok(self) -> bool:
            # Defend against DNS rebinding: only answer for our own address.
            return self.headers.get("Host", "") in (f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}")

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _authed(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Token", ""), token)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            path, _, query = self.path.partition("?")
            lib.last_ping = time.monotonic()
            if path in ("/", "/index.html"):
                html = (WEB_DIR / "index.html").read_text("utf-8").replace("__TOKEN__", token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            if path == "/api/state":
                m = re.search(r"since=(\d+)", query)
                return self._json(lib.state(int(m.group(1)) if m else 0))
            if path == "/api/settings":
                return self._json(lib.settings())
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._host_ok() or not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            lib.last_ping = time.monotonic()
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            if self.path == "/api/settings":
                err = lib.save_settings(data)
                return self._json({"ok": not err, "error": err})
            if self.path == "/api/test":
                return self._json({"results": lib.test_settings(data)})
            if self.path == "/api/refresh":
                lib.refresh()
                return self._json({"ok": True})
            if self.path == "/api/launch":
                appid = int(data.get("appid", 0))
                action = "install" if data.get("install") else "run"
                if appid > 0:
                    open_url(f"steam://{action}/{appid}")
                return self._json({"ok": appid > 0})
            if self.path == "/api/open":
                url = str(data.get("url", ""))
                if re.match(r"https://(store\.steampowered\.com|steamdb\.info|steamcommunity\.com)/", url):
                    open_url(url)
                    return self._json({"ok": True})
                return self._json({"ok": False}, 400)
            if self.path == "/api/bye":
                # Page closed: quit in a few seconds unless it comes back (reload).
                lib.last_ping = time.monotonic() - (IDLE_TIMEOUT - 6) if data.get("closing") else time.monotonic()
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)

    return Handler


def start_server(lib: Library, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    token = secrets.token_urlsafe(24)
    port_ref = [port]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(lib, token, port_ref))
    httpd.daemon_threads = True
    port_ref[0] = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port_ref[0]}/"
