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
- Featured stations are editable: right-click any station row to add it to or remove it from Featured
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
- Lyrics for the current track, fetched on demand and reloaded when the track changes
- An audio visualizer fed by `cava` (required for this feature) with an on/off toggle, eight styles (bars, bars-mirror, voice-gradient, bars-meter, VU ladder, vertical VU, VU spectrum, VU dots), and mono/stereo plus linear/decibel display options
- A bar widget that shows an accent note glyph, the current `title · artist`, a hover tooltip, left-click to open the panel, middle-click to toggle play/pause, and a right-click transport menu
- Local Last Played history with its own Clear button
- Keyboard control of the panel: move a list cursor, seek, drive the transport, and close the panel
- Library browsing from Home, Recent, Liked, Songs, Albums, and Artists, with album and artist pages that open in place

## Using the panel

### The bar widget

- The music-note glyph is always visible; it turns the accent colour while a track is playing.
- While playing it also shows `<title> · <artist>`, truncated at about 40 characters. When idle or paused only the icon shows.
- The tooltip reads `YouTube Music — not logged in` when there is no status, `YouTube Music — idle` when stopped, and otherwise `<title> — <artist> — <album>`, with ` — paused` appended while paused.
- Left-click opens or closes the panel. Middle-click toggles play/pause. Right-click opens a transport menu with Play/Pause, Previous, Next, Stop, and Open player/Close player.
- There is no scroll gesture on the bar.

### Tabs

Tabs appear in this order; **Playlists** and **Library** show up only while logged in:

1. **Up Next** — the play queue, labelled `Up Next (N)` when it holds tracks.
2. **Search**
3. **Last Played** — local play history, with a Clear button.
4. **Playlists**
5. **Library**

Clicking the tab you are already on pops one level: an open playlist, album, or artist returns to its list, and clicking it again at the top level collapses the tab. When only one tab is available it stays open.

### Now-playing card

- The card shows 96×96 album art (display only — it is not clickable), a `NOW PLAYING` label, the title, the artist, and elapsed/total time.
- While nothing is playing the same card doubles as a `LAST PLAYED` card with a single replay button for the most recent track.
- The transport row appears only while playing or paused: Previous, Play/Pause, Next, Like (heart, accent while the current track is liked; clicking likes or unlikes), Dislike, Shuffle (a real toggle, accent while on), Stop, Repeat/Loop (toggles loop-all, accent while on), and Mute (accent while muted; unmuting restores the previous volume).
- The seek slider runs from 0 to the track duration, shows the scrub position while dragging, and seeks when released.
- The volume slider runs 0–100 with a percentage label beside it; a muted player reads 0.
- Right-clicking the card opens the current track's menu.

### Search

- The `Lookup tunes...` field searches as you type (about a 450 ms debounce) and on Enter. The Search button runs the search immediately and the × (`Clear lookup`) button clears the field and the results.
- A Log out button sits at the right of the search row while you are logged in.
- Once there is a query, four scope pills appear — Songs, Albums, Artists, Playlists — and switching one runs the search again.
- On the Songs scope, **Play all** and **Queue all** act on every song listed.
- Result rows show a kind icon, the title, an `artist · album` subtitle, and, for songs, the duration, a Play button, and a Start-mix button. Albums, artists, and playlists end in a `›` chevron and open on click, in place under the Search tab; albums and artists come with a **Back** button that returns to the results.
- **Select** / **Done** toggles select mode. With rows selected an action bar shows `N selected`, `Add to playlist…`, and — only when every selected row is a song — `Queue`. The hint reads "Click rows to select. Shift-click for a range, Ctrl-click to toggle."
- An empty result set shows `No results for "<query>"`.

### Playlists

- A `New playlist name` field and a Create button make a new private playlist.
- Each playlist is a row with its title and description; clicking one opens its tracks.
- Inside an open playlist, the ⋮ options button offers **Rename playlist**, **Make public**, **Make private**, **Make unlisted**, and **Delete playlist…** (deleting asks for confirmation).
- The ▶ button queues and plays the whole playlist.
- Track rows have a Play button and a Start-mix button. Right-clicking a track offers Play now, Play next, Add to queue, Start mix, Like, Add to playlist…, Remove from playlist, and Move up / Move down where they apply.
- **Add to playlist…** opens a searchable picker (`Search or create…`): choose a playlist, or type a name that is not in the list to get `Create "<name>"`, which creates the playlist and adds the tracks straight away.
- Adding reports what happened, including tracks that were already in the playlist and anything YouTube refused to add.

### Library

- Six source buttons: **Home**, **Recent**, **Liked**, **Songs**, **Albums**, **Artists**.
- Album and artist pages open in place with cover art, a title/subtitle line, a metadata line (albums: year · track count · duration; artists: subscribers · monthly listeners), and a description you can click to expand.
- **Back** returns to the previous browse level (the history keeps up to 8 levels), and **Play all** / **Queue all** act on the page's tracks.
- Album pages have a Save/Remove button that adds or removes the album from your YouTube Music library; artist pages have **Start radio**, and similar artists are listed beneath the artist's tracks.
- Album rows also offer **Save to library** in their right-click menu.

### Up Next

- The current row is highlighted and shows a play glyph instead of its number; every row has a Jump-to-track button. The list scrolls to the current row when the tab opens or the track changes, so the playing track (and the track after it) is always in view even when it sits deep in the queue.
- The track list scrolls on its own: the now-playing card, the tab strip, and the UP NEXT header stay fixed while the rows move under them. Every other tab works the same way — the now-playing card, tab strip, and that tab's own header or action row stay put while only its list scrolls.
- Right-clicking a row offers Move up, Move down, and Remove from queue.
- **Clear** removes every upcoming track (or clears the saved queue when nothing is playing). **Save** saves the queue as a new playlist. **Select** enables multi-select, and the action bar then shows `N selected`, `Add to playlist…`, and **Remove**.
- While playback is stopped the last queue is still shown from the saved session, with a **Resume** button; opening the panel restores that queue paused.
- Empty state: `Queue is empty. Right-click any track and choose "Add to queue".`

### Lyrics

- A **Lyrics** button (visible while playing) opens the lyrics section and is accent-coloured while the section is open.
- The section shows `Loading…` while fetching and `No lyrics available.` when there are none, and reloads when the track changes. The × button closes it.

### Status line

- Every action's result appears in a status line at the bottom of the panel and fades after four seconds — additions with their counts, errors, and timeouts alike.

### Login and account

- Logged out, the panel shows a `Not logged in` card with a **Login to YouTube Music** button. It opens a terminal and runs `yt-music-ctl login`, which reads the login cookies from Chromium first, then Firefox, and otherwise falls back to pasting request headers by hand.
- After the browser opens, the panel polls for up to about 100 seconds and then reports that the login was not detected.
- **Log out** (in the Search row) removes the locally stored authentication only; it does not touch your browser or your YouTube account.

## Reference

### Keyboard shortcuts

| Key | Action |
| --- | --- |
| `j` / `↓` | Move the list cursor down |
| `k` / `↑` | Move the list cursor up |
| `h` / `←` | Seek 5 seconds back during playback |
| `l` / `→` | Seek 5 seconds forward during playback |
| `Enter` / `Space` | Activate: play the highlighted item, jump to the highlighted queue entry, or open the highlighted album/artist/playlist; in the delete confirmation, confirms |
| `x` / `X` | Delete: removes the highlighted queue entry or the highlighted playlist track, otherwise clears the search |
| `s` | Stop |
| `m` | Mute toggle |
| `r` | Repeat/loop toggle |
| `f` | Shuffle |
| `/` | Switch to the Search tab and focus the search box |
| `c` | Close the panel |
| `Esc` | Close the panel, or dismiss the delete confirmation |

Shortcuts pause while a text field or an open menu has focus. Ctrl-click toggles a row's selection and Shift-click selects a range, in the Search results and the Up Next list. Tab does not navigate in this panel, and there is no scroll-to-seek or scroll-to-volume gesture.

### Right-click menus

- **Song** (search results, library, last played): Play now, Play next, Add to queue, Start mix, Like, Add to playlist… — the account actions only while logged in.
- **Now-playing card**: the same list, plus Dislike; dislike is only offered here.
- **Track in an open playlist**: the song list above, plus Remove from playlist and Move up / Move down where they apply.
- **Album row**: Album info, Save to library (logged in), Play all, Add all to queue, Add all to playlist…, Open.
- **Artist row**: Artist info, Start radio, Play all, Add all to queue, Add all to playlist…, Open.
- **Playlist row**: Play all, Add all to queue, Add all to playlist…, Open.
- **Queue row**: Move up, Move down, Remove from queue — plus the song actions when the row is a normal track.
- **Station row**: Play station, Add to / Remove from Featured, Add to / Remove from Favorites.

### Command line

`yt-music-ctl` drives everything the panel does; run it with no arguments to get the built-in help. Most commands print JSON on stdout so they are easy to script — `login` walks you through the browser flow instead.

- **Playback:** `play`, `play-next`, `queue-add`, `pause`, `resume`, `toggle`, `next`, `prev`, `seek`, `seek-pct`, `volume`, `stop`, `loop`, `shuffle`
- **Engagement:** `like`, `dislike`, `unlike`
- **Account:** `login`, `logout`
- **Library/search:** `search` (`-f songs|albums|artists|playlists`), `playlists`, `create-playlist`, `playlist`, `liked`, `library`, `album`, `album-status`, `album-save`, `album-remove`, `artist`, `radio`, `home`, `history`, `last-played`, `restore`
- **Playlist editing:** `playlist-add`, `playlist-add-items`, `playlist-edit`, `playlist-delete`, `playlist-move`, `remove`
- **Queue:** `queue-list`, `queue-clear`, `queue-jump`, `queue-remove`, `queue-remove-keys`, `queue-move`, `queue`, `enqueue`, `enqueue-files`
- **Media/cache:** `thumbnail`, `image`, `precache`, `lyrics`, `mix`
- **Service:** `status`, `daemon`, `watch` (alias), `ensure-daemon`, `daemon-stop`, `doctor`

The metadata read commands (`search`, `library`, `home`, `history`, `last-played`, `album`, `artist`, `playlist`, `mix`, `lyrics`) accept `-r`/`--refresh` anywhere to bypass the on-disk metadata cache.

## Requirements

- Omarchy with the Quickshell bar
- Python 3
- MPV
- `yt-dlp`
- `mpv-mpris` (optional, but recommended: enables the desktop media keys and the Omarchy media widget)
- `cava` (optional, but required for the audio visualizer)
- `libnotify`/`notify-send` for track-change notifications (part of a standard Omarchy install)
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
