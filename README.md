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
- The Up Next tab is always visible (with an empty state when nothing is queued) and, while playback is stopped, it shows the last queue with a Resume action
- Albums, artists, and playlists open in place under the tab you are on, so the tab bar keeps matching what you see and Back returns to the list you came from
- Albums can be saved to or removed from your YouTube Music library from the album view (or the album row's right-click menu)
- The Playlists tab lists your playlists as rows (title and description) instead of a drop-down
- Every action's result appears in a status line at the bottom of the panel and fades after four seconds
- Album artwork and progress display
- Panel chrome follows the active Omarchy theme through the shared shell UI kit (tokens, hover/selection states, section headers)
- MPRIS: playback is controllable from the desktop through `mpv-mpris` (media keys, Omarchy's media widget), with title, artist, and album art reported to any MPRIS client
- Private per-user runtime state and MPV IPC socket

## Requirements

- Omarchy with the Quickshell bar
- Python 3
- MPV
- `yt-dlp`
- `mpv-mpris` (optional, but recommended: enables the desktop media keys and the Omarchy media widget)
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

## Desktop integration

Playback is published on D-Bus as `org.mpris.MediaPlayer2.mpv` by
[`mpv-mpris`](https://github.com/hoyon/mpv-mpris), which mpv loads
automatically from `/etc/mpv/scripts/mpris.so`. That is what lets the
desktop media keys (bound to `omarchy-shell media ...`) and Omarchy's media
widget control this player, and it is how MPRIS clients get the track title,
artist, and album art.

```bash
sudo pacman -S mpv-mpris
```

MPRIS identifies the player as `mpv` (`playerctl -p mpv ...`). The notice
printed by `./install.sh` and `yt-music-ctl doctor` both report whether it
is installed.

## Testing

`scripts/smoke.sh` is a PASS/FAIL smoke test for every backend command.

```bash
scripts/smoke.sh              # read-only, network navigation, cache, usage errors
scripts/smoke.sh --full       # plus transport, queue, radio and precache (plays audio)
scripts/smoke.sh --mutating   # opt-in, reversible account mutations
                              # (throwaway playlist + like/unlike)
```

`scripts/model_test.js` (Node) covers the pure list/selection helpers:

```bash
node scripts/model_test.js
```

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
