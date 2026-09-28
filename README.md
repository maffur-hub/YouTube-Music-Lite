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
- Select multiple search results with Ctrl-click (toggle) and Shift-click (range), then add them all to a playlist in one go
- Album artwork and progress display
- Private per-user runtime state and MPV IPC socket

## Requirements

- Omarchy with the Quickshell bar
- Python 3
- MPV
- `yt-dlp`
- A Chromium-based browser or Firefox logged into YouTube Music

## Install

```bash
git clone https://github.com/stevenwtlafrance-ship-it/YouTube-Music-Lite.git
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

## License

MIT. See [LICENSE](LICENSE).
