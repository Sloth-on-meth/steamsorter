# SteamSorter

A tiny desktop app to answer "what should I play?" when you own thousands of Steam
games. It lists your library **plus everything shared with you via Steam Family**
and lets you sort and filter by user rating, genre, features (co-op, controller
support, ...), playtime, release date and more. If you still can't choose, hit
**🎲 Pick something for me**.

- Pure Python + Tkinter, no third-party packages. Works on Linux and Windows.
- Ratings and genres are cached locally, so only the first run is slow.

## Run it

You need Python 3.10+ with Tkinter.

- **Linux:** Tkinter is usually a separate package:
  - Debian/Ubuntu: `sudo apt install python3-tk`
  - Fedora: `sudo dnf install python3-tkinter`
  - Arch: `sudo pacman -S tk`
- **Windows:** the python.org installer already includes Tkinter.

```bash
git clone https://github.com/sloth-on-meth/steamsorter.git
cd steamsorter
python3 -m steamsorter
```

## Setup (first launch opens Settings)

| Field | What to put there |
|---|---|
| **Steam Web API key** | Get one at <https://steamcommunity.com/dev/apikey> (enter any domain name). |
| **Your Steam ID** | Your SteamID64, your custom URL name, or just paste your profile link. |
| **Store access token** *(for Steam Family)* | Log in to the Steam store in your browser, open <https://store.steampowered.com/pointssummary/ajaxgetasyncconfig> and copy the `webapi_token` value. Pasting the whole JSON works too. |

The access token is the only way to see family-shared games: Steam's family
endpoints don't accept a normal API key. The token **expires after about 24 hours**.
When it does, your own games still load and the status bar asks for a fresh one.
If you set the token, the API key and Steam ID are optional because the token
already identifies you.

Settings and the cache are stored in `~/.config/steamsorter/` on Linux and
`%APPDATA%\steamsorter\` on Windows. The key and token are saved in plain
text in `config.json` (readable only by your user on Linux).

## How it works

1. Your library comes from `IPlayerService/GetOwnedGames`, and family games from
   `IFamilyGroupsService/GetSharedLibraryApps`. Family games appear in blue with
   the owner's name.
2. Two background workers fill in data for each game:
   - **Ratings** come from the store reviews endpoint (fast, about 3 games/second).
   - **Genres, features, release date and Metacritic** come from the store
     `appdetails` endpoint. Steam limits it to about 200 requests per 5 minutes,
     so **the first run takes about 1.6 s per game** (around an hour for 2,000
     games). You can use the app the whole time: games you've played most are
     fetched first and the list fills in as data arrives. Everything is cached,
     and data older than 30 days is refreshed.

### Columns

- **Rating**: % positive Steam user reviews.
- **Score**: a SteamDB-style score that pulls games with few reviews towards 50%,
  so a game with 98% from 50 reviews doesn't outrank 95% from 100k. This is the
  default sort and the best one for "what's actually good".
- **Steam says**: Steam's own label ("Overwhelmingly Positive", ...).

Click any column header to sort it, and click again to reverse. Unknown values
always go to the bottom.

### Tips

- Double-click a game (or press Enter) to launch or install it through Steam
  (`steam://run/<appid>`). Right-click for the store page or SteamDB.
- "Played: Never played" + "Min reviews: 1000" + sort by Score gives you your
  best unplayed games.
- Steam doesn't report your playtime for family-shared games, so for those
  "played" is based on when you last launched them.

## Optional: single-file Linux binary

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name steamsorter steamsorter/__main__.py
./dist/steamsorter
```

## Tests

```bash
python3 -m unittest discover -s tests
```
