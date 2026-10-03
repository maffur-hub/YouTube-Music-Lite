# Review findings and triage (2026-10-03)

A read-only review pass over the whole plugin (~11.5k lines: `backend/yt_music.py`,
`Panel.qml`, `Model.js`, `BarWidget.qml`, `scripts/smoke.sh`), split into a backend
scan and a frontend scan. Findings are grouped by disposition. This file is the
working triage for the fixes; it is not user documentation and may be deleted
once the backlog is closed.

## Dispositions

| Bucket | Meaning |
| --- | --- |
| **Quick win** | Small, localised, low-risk fix; regression covered where possible. |
| **Needs design** | Correctness-sensitive; wants a focused design pass before editing. |
| **Won't fix** | Out of scope for a personal Omarchy widget (e.g. i18n). |

---

## Quick wins (in progress)

### Backend

- [x] **QW-B1 `cmd_volume` crashes on non-numeric input** — `backend/yt_music.py:3878`.
  `int(args[0])` is unguarded, so `yt-music-ctl volume abc` raises a traceback and
  breaks the "handled failure" contract every other command honours. `smoke.sh`
  already asserts this (`check_usage "volume abc"`) and currently only avoids a
  failure because it is skipped without a player. Wrap the conversion and `fail()`
  like `cmd_seek_pct`.
- [x] **QW-B2 Auth dir/file are not forced private** — `backend/yt_music.py:1216`,
  `:1251`, `:1266`. `~/.config/yt-music/` holds browser session cookies (`SID`,
  `__Secure-3PSID`, `LOGIN_INFO`) but is created with `makedirs(..., exist_ok=True)`
  and no mode, and the manual `ytmusicapi.setup()` path does not guarantee 0600.
  State/cache dirs are already hardened via `_ensure_private_dir`; make auth match.
- [x] **QW-B3 Background-refresh sentinel is namespace-agnostic** —
  `backend/yt_music.py:426`. A single `refresh.lock` suppresses refreshes for
  *every* cache namespace for 120 s, so a stale read of one screen blocks the
  refresh of the next. Key the lock on `(namespace, args)` and prune old locks.

### Frontend

- [x] **QW-F1 Stale library response overwrites the screen** — `Panel.qml:1551`
  (`libraryProc.onExited`), `:820` (`libraryBack`). A slow album/artist load that
  lands after the user navigates Back repaints the new screen with the old data.
  Add a request token to `libraryProc`, capture it when starting, and discard
  responses whose token no longer matches.
- [x] **QW-F2 `openPlaylist` / `search` restarts a running process** — `Panel.qml:485`,
  `:583`, `:1298`, `:1315`. `startProcess()` clears the output buffer immediately, so
  a restart mid-flight lets late chunks from the previous request contaminate the
  new response (JSON parse failure), and the wrong `onExited` clears the spinner.
  Queue the newest request and start it from `onExited` (plus a token check).
- [x] **QW-F3 `refreshing` is shared by two unrelated processes** — `Panel.qml:347`,
  `:1285`. `playlistsProc.onExited` clears `refreshing` even while `statusProc` is
  still running, so `autoRefresh` can start a second status fetch and reset its
  buffer. Guard `refresh()` on `statusProc.running`; let only `statusProc` clear
  the flag.

---

## Needs design (blocked on a focused pass)

- [x] **`validate_auth()` does a live network call on every authenticated
  command** — `backend/yt_music.py:803`, `:874`. Doubles API calls and blocks while
  offline; the stale-cache fallback is largely bypassed. Needs a short-lived
  validity cache / cheaper check.
- [x] **Daemon attaches to any user-owned `mpv.sock`** — `backend/yt_music.py:4140`.
  No proof the socket belongs to *this* plugin's mpv, so it can mutate a foreign
  mpv queue. Needs the same identity/pidfile check `mpv_kill` uses.
- [x] **`_confirmed_ids` mis-handles partial responses** — `backend/yt_music.py:1383`.
  A response with `playlistEditResults` present but no `setVideoId` claims
  everything was skipped, while an unexpected shape claims everything landed.
- [x] **Playlist ops only see the first 100 tracks** — `backend/yt_music.py:1422`,
  `:1587`, `:1874`. `get_playlist(limit=100)` makes large playlists report false
  duplicates and leaves the tail unremovable/unreorderable. Needs pagination.
- [x] **Missing `yt-music-ctl` leaves `busy` stuck forever** — `Panel.qml:388`.
  Quickshell does not emit `started`/`exited` on `FailedToStart`, and every timeout
  timer arms in `onStarted`, so `busy`/`refreshing` never clear. Needs an
  `onRunningChanged`/error fallback.

## Lower severity (backlog)

All lower-severity items are now either fixed or deliberately declined.

- [x] `refresh_auth_headers` non-mutating + single quote-aware cookie parser;
  daemon/precache exceptions logged (throttled); `cmd_image` symlink symmetry;
  `install.sh` realpath guard; `cmd_next`/`cmd_prev`/`cmd_queue_jump` wait for the
  actual track change.
- [x] Frontend: stale index-based queue remove/move; loading states for
  library/album/artist/playlist; keyboard-navigable menus; thumbnail retry;
  `Delete` no longer clears search; paused state shown in the bar.
- **Declined (with reason):** panel unload + `Repeater`→`ListView` virtualization.
  High regression risk in a 4,600-line panel, needs live-shell tuning to size
  delegates, and low payoff on the target hardware. Revisit only if the panel is
  observed to be sluggish.
- **Declined:** i18n / `qsTr()` coverage — the plugin ships English-only.

## Won't fix

- `i18n` / `qsTr()` coverage — the plugin ships English-only.
