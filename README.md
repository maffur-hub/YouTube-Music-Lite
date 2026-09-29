# YouTube Music Lite for Omarchy

A free YouTube Music player for the Omarchy bar. It uses `ytmusicapi` for
library/search actions and MPV with `yt-dlp` for audio playback.

## Features

- Browser-cookie login with a manual-header fallback
- Search while typing
- Playlist browsing with explicit playlist playback
- Create private playlists from the player
- Play, pause, previous, next, shuffle, like, and dislike controls
- Remove tracks from playlists with right-click
- Rename a playlist and switch its privacy (public, private, or unlisted) from the playlist options menu
- Delete a playlist after an explicit confirmation dialog
- Reorder playlist tracks with right-click Move up / Move down
- Save the current queue as a new playlist
- Select multiple search results with Ctrl-click (toggle) and Shift-click (range), then add them all to a playlist in one go from a searchable picker that also creates a new playlist inline from the name you type
- Select whole albums, artists, or playlists the same way, and add all of their tracks to a playlist from the row's right-click menu
- Adding selected tracks reports what happened, including which tracks were already in the playlist and anything YouTube refused to add
- Select multiple Up Next entries to add them to a playlist or remove them together
- Albums, artists, and playlists open in place under the tab you are on, so the tab bar keeps matching what you see and Back returns to the list you came from
- The Playlists tab lists your playlists as rows (title and description) instead of a drop-down
- Every action's result appears in a status line at the bottom of the panel and fades after four seconds
- Album artwork and progress display
- Panel chrome follows the active Omarchy theme through the shared shell UI kit (tokens, hover/selection states, section headers)
- Private per-user runtime state and MPV IPC socket

## Requirements

- Omarchy with the Quickshell bar
- Python 3
- MPV
- `yt-dlp`
- A Chromium-based browser or Firefox logged into YouTube Music

## Install

```bash
git clone https://github.com/maffur-hub/YouTube-Music-Lite.git
cd YouTube-Music-Lite
./install.sh
```

The installer creates a private virtual environment under
`~/.local/share/yt-music`, installs the exact hash-verified Python dependency
lock, installs the bar plugin, and enables it in the Omarchy bar. The installer
does not upgrade pip or download unpinned dependencies.

Log in after installation:

```bash
yt-music-ctl login
```

Click the music widget in the bar to open the player.

## Uninstall

```bash
./install.sh --uninstall
```

This removes the plugin, launcher, and virtual environment. Authentication
data under `~/.config/yt-music` is left untouched so it can be removed or
reused separately.

## Privacy

Authentication headers are stored locally in `~/.config/yt-music/auth.json`
with owner-only permissions. No credentials, playlists, or playback state
are included in this repository.

## Credits

This is a fork of [YouTube Music Lite](https://github.com/stevenwtlafrance-ship-it/YouTube-Music-Lite)
by stevenwtlafrance-ship-it, substantially expanded with playlist management,
multi-select, library browsing, lyrics, offline caching, and a persistent
status daemon.

## License

MIT. See [LICENSE](LICENSE). Original work Copyright (c) 2026
stevenwtlafrance-ship-it; modifications Copyright (c) 2026 maffur-hub.
