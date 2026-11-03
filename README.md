# SteamSorter

A tiny desktop app to answer "what should I play?" when you own thousands of Steam
games. It shows your library **plus everything shared with you via Steam Family**
as a cover-art grid, and lets you sort and filter by user rating, genre, features
(co-op, controller support, ...), Steam user tags, playtime, release date and more. Still stuck?
Hit **Pick something for me** and spin the roulette.

- **Linux-first**: every game shows its **ProtonDB** rating (Platinum/Gold/Silver/
  Bronze/Borked) or a 🐧 *Native* badge, plus Steam Deck Verified/Playable. You can
  filter by minimum tier, sort by it, and borked games are hidden by default.
- **Controller filters**: DualSense **adaptive triggers** and **haptics** (from
  PCGamingWiki), plus Steam's own PlayStation/Xbox/controller support categories.
- One-click **Quick picks**: Best unplayed, Hidden gems, Runs great on Linux,
  DualSense showcase, Co-op night, Family vault, Forgotten favourites.
- Each game page uses its Steam library hero art, shows ratings and stats, and has a Play button that launches it in Steam.
- Pure Python standard library, nothing to `pip install`. Works on Linux and Windows.
- Everything is cached locally, so later launches are instant.

## Run it

You need Python 3.10+ and a Chromium-based browser (Chromium, Chrome, Brave, Edge
or Vivaldi) for the app window:

```bash
git clone https://github.com/sloth-on-meth/steamsorter.git
cd steamsorter
python3 -m steamsorter
```

The UI opens in its own chromeless window and the app quits when you close it.
Without a Chromium-based browser it opens in your default browser instead. You can
also `pip install pywebview` for a native window.

Options: `--browser` (use your normal browser), `--no-open` (just print the URL),
`--port N`.

## Setup (first launch asks for this)

| Field | What to put there |
|---|---|
| **Steam Family access token** *(recommended)* | Log in to the Steam store in your normal browser, open <https://store.steampowered.com/pointssummary/ajaxgetasyncconfig>, press Ctrl+A, Ctrl+C and paste the whole page. The app extracts the `webapi_token` and your Steam ID from it. |
| **Steam Web API key** *(optional)* | <https://steamcommunity.com/dev/apikey> (enter any domain name). Lets your own games load even after the token expires. |
| **Steam ID** | Filled in automatically from the token. Otherwise use your SteamID64, custom URL name, or profile link. |

The token is the only way to see family-shared games, because Steam's family
endpoints don't accept a normal API key. It **expires after about 24 hours**. When
it does, the app tells you and keeps showing your own games (if you set an API key).

Settings and the cache are stored in `~/.config/steamsorter/` on Linux and
`%APPDATA%\steamsorter\` on Windows. The key and token are saved in plain
text in `config.json` (readable only by your user on Linux). The UI is served
only on `127.0.0.1`, with a random per-session token, so other websites can't
talk to it.

## How it works

1. Your library comes from `IPlayerService/GetOwnedGames`, and family games from
   `IFamilyGroupsService/GetSharedLibraryApps`. Family games get a 👪 badge with
   the owner's name.
2. Ratings, tags, features, release dates and cover art come from Steam's store
   browse API (`IStoreBrowseService/GetItems`), **100 games per request**, so even
   a few thousand games load in under a minute. Everything is cached locally and
   refreshed after 30 days. If that API ever fails, the app falls back to the old
   per-game store endpoints, which are much slower (about 1.6 s per game).

3. ProtonDB ratings come from `protondb.com/api/v1/reports/summaries/<appid>.json`.
   There's no batch API, so they're fetched one game at a time in the background
   (best-rated games first, about 8 per second) and cached for 14 days. Native
   Linux and Steam Deck status come from Steam with the rest of the store data.

4. DualSense adaptive-trigger and haptics support come from PCGamingWiki's
   database (a few bulk queries, refreshed weekly). Steam itself doesn't track
   them. The wiki is community-maintained, so it's good but not complete. Steam's
   controller categories (PlayStation controller support etc.) come from Steam's
   own category list, so their names match what the store shows.

### Troubleshooting

Open **Settings → Test connection**. It checks each step (token, your library,
Steam Family, store data, PCGamingWiki) and shows exactly what each service answered. Family problems
are almost always an expired token, or a token copied while logged in as a
different account.

### Ratings

- The **% badge** shows positive Steam user reviews. Green is 85%+, lime 70%+,
  amber 50%+, and red is below that.
- **Best rated (weighted)**, the default sort, uses a SteamDB-style score that pulls
  games with few reviews towards 50%. That way a game with 98% from 50 reviews
  doesn't outrank 95% from 100k.

### Tips

- Keyboard: `/` searches, `R` spins the roulette, and `Esc` closes dialogs.
- Click a tag on a game's page to filter by it.
- Steam doesn't report your playtime for family-shared games, so for those
  "played" is based on when you last launched them.

## Optional: single-file Linux binary

```bash
pip install pyinstaller
pyinstaller --onefile --name steamsorter --add-data steamsorter/web:steamsorter/web steamsorter/__main__.py
./dist/steamsorter
```

## Tests

```bash
python3 -m unittest discover -s tests
```
