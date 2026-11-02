"""Tkinter GUI for SteamSorter."""
from __future__ import annotations

import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from tkinter import messagebox, ttk

from . import __version__
from .steam import SteamClient, SteamError, rating_score, steamid_from_token, token_expiry
from .store import Cache, Config, Game, is_stale

ALL = "All"

# (column id, heading, width, anchor, sort key, formatter)
COLUMNS = [
    ("name", "Name", 300, "w", lambda g: g.name.lower(), lambda g: g.name),
    ("rating", "Rating", 70, "e", lambda g: g.rating, lambda g: f"{g.rating * 100:.0f}%" if g.rating is not None else ""),
    ("score", "Score", 60, "e", lambda g: rating_score(g.positive, g.negative),
     lambda g: f"{rating_score(g.positive, g.negative) * 100:.1f}" if rating_score(g.positive, g.negative) is not None else ""),
    ("reviews", "Reviews", 80, "e", lambda g: g.total_reviews, lambda g: f"{g.total_reviews:,}" if g.total_reviews is not None else ""),
    ("verdict", "Steam says", 120, "w", lambda g: rating_score(g.positive, g.negative), lambda g: g.review_desc),
    ("genres", "Genres", 170, "w", lambda g: ", ".join(g.genres).lower() or None, lambda g: ", ".join(g.genres)),
    ("hours", "Hours", 60, "e", lambda g: g.playtime, lambda g: f"{g.playtime / 60:.1f}" if g.playtime else ""),
    ("last", "Last played", 90, "w", lambda g: g.last_played or None,
     lambda g: datetime.fromtimestamp(g.last_played).strftime("%Y-%m-%d") if g.last_played else ""),
    ("released", "Released", 90, "w", lambda g: g.release_sort or None, lambda g: g.release_sort[:4] if g.release_sort else ""),
    ("meta", "Metacritic", 75, "e", lambda g: g.metacritic, lambda g: str(g.metacritic) if g.metacritic else ""),
    ("owner", "Library", 120, "w", None, None),  # filled in by the app (needs names)
]
COL = {c[0]: c for c in COLUMNS}


def open_url(url: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        webbrowser.open(url)


class Enricher:
    """Background threads that fetch reviews and store details for apps."""

    def __init__(self, client: SteamClient, out: queue.Queue):
        self.client = client
        self.out = out
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self, need_reviews: list[int], need_details: list[int]) -> None:
        self.threads = [
            threading.Thread(target=self._run, args=("reviews", need_reviews, self.client.app_reviews), daemon=True),
            threading.Thread(target=self._run, args=("details", need_details, self.client.app_details), daemon=True),
        ]
        for t in self.threads:
            t.start()

    def cancel(self) -> None:
        self.stop.set()

    def _run(self, kind: str, appids: list[int], fetch) -> None:
        for appid in appids:
            if self.stop.is_set():
                return
            try:
                self.out.put((kind, appid, fetch(appid, self.stop)))
            except SteamError as e:
                if self.stop.is_set():
                    return
                self.out.put(("fetch_error", kind, appid, str(e)))
        self.out.put(("done", kind))


class SettingsDialog(tk.Toplevel):
    def __init__(self, master: "App", cfg: Config):
        super().__init__(master)
        self.title("SteamSorter settings")
        self.transient(master)
        self.resizable(True, False)
        self.result: Config | None = None
        self.vars = {
            "api_key": tk.StringVar(value=cfg.api_key),
            "steam_id": tk.StringVar(value=cfg.steam_id),
            "family_token": tk.StringVar(value=cfg.family_token),
        }
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        rows = [
            ("Steam Web API key", "api_key",
             "Get one at steamcommunity.com/dev/apikey (any domain name works).",
             "https://steamcommunity.com/dev/apikey"),
            ("Your Steam ID", "steam_id",
             "SteamID64, custom URL name, or your profile link.", None),
            ("Store access token\n(for Steam Family)", "family_token",
             "While logged in to the Steam store in your browser, open the link and copy the\n"
             "value of \"webapi_token\". It expires after about a day; paste a fresh one when it does.\n"
             "Optional: without it only your own games are listed.",
             "https://store.steampowered.com/pointssummary/ajaxgetasyncconfig"),
        ]
        r = 0
        for label, key, hint, link in rows:
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="nw", padx=(0, 8), pady=(6, 0))
            e = ttk.Entry(f, textvariable=self.vars[key], width=60, show="•" if key != "steam_id" else "")
            e.grid(row=r, column=1, sticky="ew", pady=(6, 0))
            if link:
                ttk.Button(f, text="Open link", command=lambda u=link: open_url(u)).grid(
                    row=r, column=2, padx=(6, 0), pady=(6, 0))
            ttk.Label(f, text=hint, foreground="gray").grid(row=r + 1, column=1, columnspan=2, sticky="w")
            r += 2

        ttk.Label(
            f,
            text="Tip: the access token alone is enough - the API key is optional if you set it.",
            foreground="gray",
        ).grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
        b = ttk.Frame(f)
        b.grid(row=r + 1, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(b, text="Save", command=self._save).pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _save(self) -> None:
        cfg = Config(**{k: v.get().strip() for k, v in self.vars.items()})
        # Users often paste the whole JSON line; extract the token.
        tok = cfg.family_token.strip().strip('"')
        if '"webapi_token"' in cfg.family_token:
            m = re.search(r'"webapi_token"\s*:\s*"([^"]+)"', cfg.family_token)
            tok = m.group(1) if m else tok
        cfg.family_token = tok
        if not cfg.steam_id and tok:
            cfg.steam_id = steamid_from_token(tok) or ""
        if not (cfg.api_key or cfg.family_token):
            messagebox.showerror("SteamSorter", "Enter an API key or an access token.", parent=self)
            return
        if not cfg.steam_id:
            messagebox.showerror("SteamSorter", "Enter your Steam ID.", parent=self)
            return
        self.result = cfg
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"SteamSorter {__version__}")
        self.geometry("1280x760")
        self.minsize(800, 450)
        self.cfg = Config.load()
        self.cache = Cache()
        self.names = self.cache.load_names()
        self.games: dict[int, Game] = {g.appid: g for g in self.cache.load_library()}
        self.visible: list[Game] = []
        self.q: queue.Queue = queue.Queue()
        self.enricher: Enricher | None = None
        self.pending = {"reviews": 0, "details": 0}
        self.sort_col, self.sort_desc = "score", True
        self.dirty = False
        self.last_refilter = 0.0
        self.status_msg = ""
        self._search_after = None

        self._build_ui()
        self.refilter()
        self._poll_id = self.after(200, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not self.cfg.steam_id or not (self.cfg.api_key or self.cfg.family_token):
            self.after(300, self.open_settings)
        else:
            self.refresh_library()

    # ---------------------------------------------------------------- UI --
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names() and sys.platform != "win32":
            style.theme_use("clam")
        style.configure("Treeview", rowheight=22)

        bar = ttk.Frame(self, padding=(8, 8, 8, 4))
        bar.pack(fill="x")

        self.v_search = tk.StringVar()
        self.v_genre = tk.StringVar(value=ALL)
        self.v_feature = tk.StringVar(value=ALL)
        self.v_source = tk.StringVar(value=ALL)
        self.v_played = tk.StringVar(value="Any")
        self.v_min_reviews = tk.StringVar(value="0")
        self.v_min_rating = tk.StringVar(value="0")
        self.v_games_only = tk.BooleanVar(value=True)

        def lbl(text):
            ttk.Label(bar, text=text).pack(side="left", padx=(8, 2))

        lbl("Search")
        e = ttk.Entry(bar, textvariable=self.v_search, width=22)
        e.pack(side="left")
        self.v_search.trace_add("write", lambda *_: self._debounced_refilter())

        lbl("Genre")
        self.cb_genre = ttk.Combobox(bar, textvariable=self.v_genre, width=16, state="readonly")
        self.cb_genre.pack(side="left")
        lbl("Feature")
        self.cb_feature = ttk.Combobox(bar, textvariable=self.v_feature, width=18, state="readonly")
        self.cb_feature.pack(side="left")
        lbl("Library")
        self.cb_source = ttk.Combobox(bar, textvariable=self.v_source, width=12, state="readonly",
                                      values=[ALL, "Mine", "Family"])
        self.cb_source.pack(side="left")
        lbl("Played")
        ttk.Combobox(bar, textvariable=self.v_played, width=12, state="readonly",
                     values=["Any", "Never played", "Played"]).pack(side="left")

        bar2 = ttk.Frame(self, padding=(8, 0, 8, 6))
        bar2.pack(fill="x")
        ttk.Label(bar2, text="Min reviews").pack(side="left", padx=(8, 2))
        ttk.Spinbox(bar2, from_=0, to=1_000_000, increment=100, width=8,
                    textvariable=self.v_min_reviews, command=self.refilter).pack(side="left")
        ttk.Label(bar2, text="Min rating %").pack(side="left", padx=(8, 2))
        ttk.Spinbox(bar2, from_=0, to=100, increment=5, width=5,
                    textvariable=self.v_min_rating, command=self.refilter).pack(side="left")
        ttk.Checkbutton(bar2, text="Games only (hide DLC/tools)", variable=self.v_games_only,
                        command=self.refilter).pack(side="left", padx=8)
        ttk.Button(bar2, text="Reset filters", command=self.reset_filters).pack(side="left")

        ttk.Button(bar2, text="Settings…", command=self.open_settings).pack(side="right")
        ttk.Button(bar2, text="Refresh library", command=self.refresh_library).pack(side="right", padx=6)
        ttk.Button(bar2, text="🎲 Pick something for me", command=self.random_pick).pack(side="right")

        for var in (self.v_genre, self.v_feature, self.v_source, self.v_played):
            var.trace_add("write", lambda *_: self.refilter())
        for var in (self.v_min_reviews, self.v_min_rating):
            var.trace_add("write", lambda *_: self._debounced_refilter())

        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8)

        tf = ttk.Frame(paned)
        cols = [c[0] for c in COLUMNS]
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse")
        for cid, head, width, anchor, *_ in COLUMNS:
            self.tree.heading(cid, text=head, command=lambda c=cid: self.sort_by(c))
            self.tree.column(cid, width=width, anchor=anchor, stretch=cid in ("name", "genres"))
        ys = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(tf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)
        self.tree.tag_configure("family", foreground="#3a6ea5")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())
        self.tree.bind("<Double-1>", lambda e: self.play_selected())
        self.tree.bind("<Return>", lambda e: self.play_selected())
        self.tree.bind("<Button-3>", self._context_menu)
        self.tree.bind("<Button-2>", self._context_menu)
        paned.add(tf, weight=5)

        # details panel
        df = ttk.Frame(paned, padding=(4, 6))
        self.d_title = ttk.Label(df, text="", font=("TkDefaultFont", 13, "bold"))
        self.d_title.grid(row=0, column=0, sticky="w")
        self.d_meta = ttk.Label(df, text="", foreground="gray")
        self.d_meta.grid(row=1, column=0, sticky="w")
        self.d_desc = ttk.Label(df, text="Select a game.", wraplength=900, justify="left")
        self.d_desc.grid(row=2, column=0, sticky="nw", pady=(4, 0))
        btns = ttk.Frame(df)
        btns.grid(row=0, column=1, rowspan=3, sticky="ne", padx=(8, 0))
        ttk.Button(btns, text="▶ Play / Install", command=self.play_selected).pack(fill="x")
        ttk.Button(btns, text="Store page", command=lambda: self._open_selected("store")).pack(fill="x", pady=3)
        ttk.Button(btns, text="SteamDB", command=lambda: self._open_selected("steamdb")).pack(fill="x")
        df.columnconfigure(0, weight=1)
        df.bind("<Configure>", lambda e: self.d_desc.configure(wraplength=max(300, e.width - 180)))
        paned.add(df, weight=1)

        self.status = ttk.Label(self, text="", anchor="w", padding=(8, 2))
        self.status.pack(fill="x", side="bottom")

        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label="Play / Install", command=self.play_selected)
        self.menu.add_command(label="Open store page", command=lambda: self._open_selected("store"))
        self.menu.add_command(label="Open on SteamDB", command=lambda: self._open_selected("steamdb"))
        self.menu.add_separator()
        self.menu.add_command(label="Copy name", command=self._copy_name)

    # ----------------------------------------------------------- helpers --
    def owner_label(self, g: Game) -> str:
        if g.source == "own":
            return "You"
        names = [self.names.get(o, o) for o in g.owners]
        return "Family: " + ", ".join(names) if names else "Family"

    def row_values(self, g: Game) -> list[str]:
        vals = []
        for cid, _h, _w, _a, _key, fmt in COLUMNS:
            vals.append(self.owner_label(g) if cid == "owner" else fmt(g))
        return vals

    def selected(self) -> Game | None:
        sel = self.tree.selection()
        return self.games.get(int(sel[0])) if sel else None

    def _int(self, var: tk.StringVar) -> int:
        try:
            return int(float(var.get() or 0))
        except ValueError:
            return 0

    def _update_filter_choices(self) -> None:
        genres, feats = set(), set()
        for g in self.games.values():
            genres.update(g.genres)
            feats.update(g.categories)
        self.cb_genre["values"] = [ALL, *sorted(genres)]
        self.cb_feature["values"] = [ALL, *sorted(feats)]

    def set_status(self, msg: str | None = None) -> None:
        if msg is not None:
            self.status_msg = msg
        total = len(self.games)
        have_r = sum(1 for g in self.games.values() if g.reviews_ts)
        have_d = sum(1 for g in self.games.values() if g.details_ts)
        fam = sum(1 for g in self.games.values() if g.source == "family")
        parts = [f"Showing {len(self.visible):,} of {total:,} ({fam:,} via family)"]
        if total and (have_r < total or have_d < total):
            parts.append(f"ratings {have_r:,}/{total:,} · genres {have_d:,}/{total:,} (fetching in background)")
        if self.status_msg:
            parts.append(self.status_msg)
        self.status.configure(text="   |   ".join(parts))

    # ----------------------------------------------------------- filters --
    def _debounced_refilter(self) -> None:
        if self._search_after:
            self.after_cancel(self._search_after)
        self._search_after = self.after(250, self.refilter)

    def reset_filters(self) -> None:
        self.v_search.set("")
        self.v_genre.set(ALL)
        self.v_feature.set(ALL)
        self.v_source.set(ALL)
        self.v_played.set("Any")
        self.v_min_reviews.set("0")
        self.v_min_rating.set("0")
        self.v_games_only.set(True)
        self.refilter()

    def matches(self, g: Game) -> bool:
        s = self.v_search.get().strip().lower()
        if s and s not in g.name.lower():
            return False
        genre = self.v_genre.get()
        if genre != ALL and genre not in g.genres:
            return False
        feat = self.v_feature.get()
        if feat != ALL and feat not in g.categories:
            return False
        src = self.v_source.get()
        if src == "Mine" and g.source != "own" or src == "Family" and g.source != "family":
            return False
        played = self.v_played.get()
        has_played = bool(g.playtime or g.last_played)
        if played == "Never played" and has_played or played == "Played" and not has_played:
            return False
        if self.v_games_only.get() and g.type not in ("", "game"):
            return False
        min_rev = self._int(self.v_min_reviews)
        if min_rev and (g.total_reviews or 0) < min_rev:
            return False
        min_rat = self._int(self.v_min_rating)
        if min_rat and (g.rating or 0) * 100 < min_rat:
            return False
        return True

    def refilter(self) -> None:
        self._search_after = None
        self.last_refilter = time.monotonic()
        self.dirty = False
        sel = self.tree.selection()
        top = self.tree.yview()[0]
        self.visible = [g for g in self.games.values() if self.matches(g)]
        self._sort_visible()
        self.tree.delete(*self.tree.get_children())
        for g in self.visible:
            self.tree.insert("", "end", iid=str(g.appid), values=self.row_values(g),
                             tags=("family",) if g.source == "family" else ())
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        self.tree.yview_moveto(top)
        self._update_headings()
        self.set_status()

    def _sort_visible(self) -> None:
        cid = self.sort_col
        if cid == "owner":
            key = lambda g: self.owner_label(g).lower()
        else:
            key = COL[cid][4]
        have = [(key(g), g) for g in self.visible]
        known = [x for x in have if x[0] is not None]
        unknown = [x[1] for x in have if x[0] is None]
        known.sort(key=lambda x: (x[0], x[1].name.lower()), reverse=self.sort_desc)
        # Unknown values always go to the bottom, alphabetically.
        self.visible = [g for _, g in known] + sorted(unknown, key=lambda g: g.name.lower())

    def sort_by(self, cid: str) -> None:
        if self.sort_col == cid:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = cid
            self.sort_desc = cid not in ("name", "genres", "owner")
        self.refilter()

    def _update_headings(self) -> None:
        for cid, head, *_ in COLUMNS:
            arrow = (" ▼" if self.sort_desc else " ▲") if cid == self.sort_col else ""
            self.tree.heading(cid, text=head + arrow)

    # ----------------------------------------------------------- actions --
    def show_details(self) -> None:
        g = self.selected()
        if not g:
            return
        self.d_title.configure(text=g.name)
        bits = []
        if g.rating is not None:
            bits.append(f"{g.review_desc or 'Reviews'}: {g.rating * 100:.0f}% of {g.total_reviews:,}")
        if g.genres:
            bits.append(", ".join(g.genres))
        if g.release:
            bits.append(f"Released {g.release}")
        if g.playtime:
            bits.append(f"You played {g.playtime / 60:.1f} h")
        bits.append(self.owner_label(g))
        self.d_meta.configure(text="  ·  ".join(bits))
        feats = ", ".join(g.categories)
        desc = g.short_desc or ("Loading details…" if not g.details_ts else "")
        self.d_desc.configure(text=desc + (f"\n\nFeatures: {feats}" if feats else ""))

    def random_pick(self) -> None:
        if not self.visible:
            messagebox.showinfo("SteamSorter", "No games match the current filters.")
            return
        g = random.choice(self.visible)
        iid = str(g.appid)
        self.tree.selection_set(iid)
        self.tree.see(iid)
        self.tree.focus(iid)

    def play_selected(self) -> None:
        g = self.selected()
        if g:
            open_url(f"steam://run/{g.appid}")

    def _open_selected(self, where: str) -> None:
        g = self.selected()
        if not g:
            return
        url = (f"https://store.steampowered.com/app/{g.appid}/" if where == "store"
               else f"https://steamdb.info/app/{g.appid}/")
        webbrowser.open(url)

    def _copy_name(self) -> None:
        g = self.selected()
        if g:
            self.clipboard_clear()
            self.clipboard_append(g.name)

    def _context_menu(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def open_settings(self) -> None:
        dlg = SettingsDialog(self, self.cfg)
        self.wait_window(dlg)
        if dlg.result:
            self.cfg = dlg.result
            self.cfg.save()
            self.refresh_library()

    # ------------------------------------------------------ data loading --
    def client(self) -> SteamClient:
        return SteamClient(self.cfg.api_key, self.cfg.family_token)

    def refresh_library(self) -> None:
        if not self.cfg.steam_id:
            self.open_settings()
            return
        self.set_status("Loading library from Steam…")
        client, cfg = self.client(), self.cfg

        def work():
            try:
                sid = client.resolve_steamid(cfg.steam_id)
                own = client.owned_games(sid)
                warn = ""
                fam: list[dict] = []
                if cfg.family_token:
                    exp = token_expiry(cfg.family_token)
                    if exp and exp < time.time():
                        warn = "Family token expired - paste a fresh one in Settings."
                    else:
                        try:
                            fam = client.family_games(sid)
                        except SteamError as e:
                            warn = f"Could not load Steam Family games ({e}). Token expired?"
                owners = sorted({o for f in fam for o in f["owners"]})
                names = client.player_names(owners) if owners else {}
                self.q.put(("library", own, fam, names, warn))
            except SteamError as e:
                self.q.put(("error", str(e)))
            except Exception as e:  # never let the thread die silently
                self.q.put(("error", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _apply_library(self, own: list[dict], fam: list[dict], names: dict, warn: str) -> None:
        new: dict[int, Game] = {}
        for src, items in (("family", fam), ("own", own)):  # own overrides family
            for it in items:
                g = Game(appid=it["appid"], name=it["name"], source=src)
                g.owners = it.get("owners", [])
                g.playtime = it.get("playtime", 0)
                g.last_played = it.get("last_played", 0)
                new[g.appid] = g
        self.names.update(names)
        self.cache.save_names(names)
        self.cache.save_library(list(new.values()))
        # Reload so every game picks up cached ratings/genres from earlier runs.
        self.games = {g.appid: g for g in self.cache.load_library()}
        self._update_filter_choices()
        self.refilter()
        self.set_status(warn or f"Library loaded at {datetime.now():%H:%M}.")
        self.start_enrichment()

    def start_enrichment(self) -> None:
        if self.enricher:
            self.enricher.cancel()
        # Fetch the games we know nothing about first, then refresh stale ones.
        gs = sorted(self.games.values(), key=lambda g: (-g.playtime, g.name.lower()))
        need_r = [g.appid for g in gs if not g.reviews_ts] + [g.appid for g in gs if g.reviews_ts and is_stale(g.reviews_ts)]
        need_d = [g.appid for g in gs if not g.details_ts] + [g.appid for g in gs if g.details_ts and is_stale(g.details_ts)]
        self.pending = {"reviews": len(need_r), "details": len(need_d)}
        if not need_r and not need_d:
            return
        self.enricher = Enricher(self.client(), self.q)
        self.enricher.start(need_r, need_d)

    def _poll(self) -> None:
        changed_details = False
        processed = 0
        try:
            while processed < 500:
                msg = self.q.get_nowait()
                processed += 1
                kind = msg[0]
                if kind == "library":
                    self._apply_library(*msg[1:])
                elif kind == "error":
                    self.set_status(f"Error: {msg[1]}")
                    if not self.games:
                        messagebox.showerror("SteamSorter", msg[1])
                elif kind in ("reviews", "details"):
                    _, appid, data = msg
                    g = self.games.get(appid)
                    if kind == "reviews":
                        self.cache.save_reviews(appid, data)
                        if g:
                            g.positive, g.negative, g.review_desc = data["positive"], data["negative"], data["review_desc"]
                            g.reviews_ts = int(time.time())
                    else:
                        self.cache.save_details(appid, data)
                        changed_details = True
                        if g:
                            d = data or {}
                            g.type = d.get("type", "")
                            g.genres = d.get("genres", [])
                            g.categories = d.get("categories", [])
                            g.release = d.get("release", "")
                            g.release_sort = d.get("release_sort", "")
                            g.metacritic = d.get("metacritic")
                            g.short_desc = d.get("short_desc", "")
                            g.details_ts = int(time.time())
                    if g and self.tree.exists(str(appid)):
                        self.tree.item(str(appid), values=self.row_values(g))
                        if self.tree.selection() == (str(appid),):
                            self.show_details()
                    self.dirty = True
                elif kind == "fetch_error":
                    self.status_msg = f"Fetch problem ({msg[1]} for app {msg[2]}): {msg[3]}"
                elif kind == "done":
                    pass
        except queue.Empty:
            pass
        if processed:
            self.cache.commit()
            if changed_details:
                self._update_filter_choices()
            self.set_status()
        # Re-apply filters/sort occasionally so newly-rated games slot in.
        if self.dirty and time.monotonic() - self.last_refilter > 10:
            self.refilter()
        self._poll_id = self.after(250, self._poll)

    def _on_close(self) -> None:
        self.after_cancel(self._poll_id)
        if self.enricher:
            self.enricher.cancel()
        try:
            self.cache.commit()
            self.cache.close()
        finally:
            self.destroy()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
