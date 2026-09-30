# YouTube Music Bar for Omarchy

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

From the Omarchy plugin marketplace:

```bash
omarchy plugin add https://github.com/maffur-hub/YouTube-Music-Lite.git --enable
~/.config/omarchy/plugins/io.github.maffur-hub.youtube-music-bar/install.sh
```

`omarchy plugin add` clones and enables the QML bar widget only. The player
itself is a Python backend, so run the bundled `install.sh` once (the second
command above). It creates a private virtual environment under
`~/.local/share/yt-music`, installs the exact hash-verified Python dependency
lock, installs the `yt-music-ctl` launcher and backend, and enables the widget
in the Omarchy bar. It does not upgrade pip or download unpinned dependencies.

Or install from a clone:

```bash
git clone https://github.com/maffur-hub/YouTube-Music-Lite.git
cd YouTube-Music-Lite
./install.sh
```

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

This removes the plugin, launcher, and virtual environment.

Marketplace installs can also be removed with
`omarchy plugin remove io.github.maffur-hub.youtube-music-bar` before running
`./install.sh --uninstall` from the plugin directory.

Authentication data under `~/.config/yt-music` is left untouched so it can be
removed or reused separately.

## Maintenance

This is a best-effort personal project with no support SLA. Issues that are
specific to one machine or environment may be closed without a fix. When
reporting a bug, include the output of `yt-music-ctl doctor`, your Omarchy
version, and `omarchy plugin list --json`; the issue template asks for these.

## Privacy

Authentication headers are stored locally in `~/.config/yt-music/auth.json`
with owner-only permissions. No credentials, playlists, or playback state
are included in this repository.

## Credits

This is a fork of [YouTube Music Lite](https://github.com/stevenwtlafrance-ship-it/YouTube-Music-Lite)
by stevenwtlafrance-ship-it, substantially expanded with playlist management,
multi-select, library browsing, lyrics, offline caching, and a persistent
status daemon. This fork is distributed as **YouTube Music Bar**.

## License

MIT. See [LICENSE](LICENSE). Original work Copyright (c) 2026
stevenwtlafrance-ship-it; modifications Copyright (c) 2026 maffur-hub.
