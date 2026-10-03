# Phase 2 evidence pack

Collected 2026-10-03 on the cheap model (step 1 of `docs/phase2-brief.md`). Read-only:
no files or accounts were modified. Everything below is paste-ready input for the
premium adjudication session (step 3). Line numbers verified against the working tree
after the Phase 1 changes.

## Provenance

- Plugin working tree: `/home/matt/.config/omarchy/plugins/io.github.maffur-hub.youtube-music-bar`
- `backend/yt_music.py` (Phase 1 applied). Installed `yt-music-ctl` symlinks to it.
- `ytmusicapi` **1.12.2** (`~/.local/share/yt-music/venv/lib/python3.14/site-packages/ytmusicapi/`)
- `quickshell` **0.3.1-1**, `qt6-base` **6.11.2-3**. C++ source is not installed on
  disk; Quickshell findings are from the installed `quickshell-io.qmltypes` plus
  upstream tag `v0.3.1` (`github.com/quickshell-mirror/quickshell`).
- Read-only live call: `get_playlist('PLZk4b3I85c8NQaGzPvC4LuRn-KQPxg9a7', limit=5)`.

## Corrections to `docs/phase2-brief.md`

- Item 3: `_confirmed_ids` is now at **`yt_music.py:1397`** (brief said `:1381`), called
  from exactly one site, **`:1451`** (add path only).
- Item 4: the brief implied several display sites could stay paged. The audit below
  shows **only one** site (playlist view, `:1852`) is display-only; the other seven
  need the full list for correctness.
- Item 3 turned out more defensible than the original review implied (see below); the
  live-capture question is the crux, not a structural bug.

---

## Item 3 — `_confirmed_ids` / `playlistEditResults` shapes

### What `add_playlist_items` actually returns (`mixins/playlists.py:436-483`)

It returns **two different top-level shapes**:

```python
474        endpoint = "browse/edit_playlist"
475        response = self._send_request(endpoint, body)
476        if "status" in response and "SUCCEEDED" in response["status"]:
477            result_dict = [
478                result_data.get("playlistEditVideoAddedResultData")
479                for result_data in response.get("playlistEditResults", [])
480            ]
481            return {"status": response["status"], "playlistEditResults": result_dict}
482        else:
483            return response
```

- **Success path (`:481`)**: `{"status": str, "playlistEditResults": [<unwrapped dict or None>, ...]}`.
  The `playlistEditVideoAddedResultData` wrapper is already stripped.
- **Non-success path (`:483`)**: the **raw** response dict, items still wrapped as
  `{"playlistEditVideoAddedResultData": {...}}`.

So `_confirmed_ids`' double read (`item.get("playlistEditVideoAddedResultData")`, then
`data = item`) is **intentional and required** for both shapes — not redundant.

### `remove_playlist_items` (`mixins/playlists.py:485-514`)

```python
513        result: str | JsonDict = response.get("status", response)
514        return result
```

Returns the status **string** on success, never `playlistEditResults`. The removal paths
do not call `_confirmed_ids`, which is correct (a bare string hits the
`not isinstance(response, dict)` branch and returns `list(requested)`).

### Token search results

- `playlistEditResults`: `mixins/playlists.py:479`, `:481`.
- `playlistEditVideoAddedResultData`: `mixins/playlists.py:478`.
- `setVideoId`: `mixins/playlists.py:67, 98, 123, 346, 383, 450, 470, 491, 495, 498, 505`;
  `parsers/playlists.py:152, 155, 161, 303, 304`.
- `STATUS_SUCCEEDED`: `enums.py:5`.
- `STATUS_FAILED`: **zero hits** anywhere in the installed package. The failed-status
  literal is not defined by the library.
- The installed wheel ships **no tests and no JSON fixtures**. Upstream `tests/data/`
  has no `edit_playlist` raw-response fixture. The only upstream assertion about the add
  response is `len(response["playlistEditResults"]) > 0` — no per-item schema.

### Live read-only track shape

Every one of the 16 tracks in the sampled playlist carries a non-empty `setVideoId`.
Track dict keys observed: `album, artists, communityVoteStatus, creditsBrowseId,
duration, duration_seconds, feedbackTokens, inLibrary, isAvailable, isExplicit,
likeStatus, listenAgainFeedbackTokens, pinnedToListenAgain, setVideoId, thumbnails,
title, videoId, videoType, views`. `setVideoId` is attached only when parsed from the
track menu (`parsers/playlists.py:160-161,303-304`); unavailable items may lack it.

### Known vs unknown

**Established from source:** the two return shapes; the double-read rationale; the
`duplicates=False` behavior (docstring `:449`: duplicates cause an error and no items
added, i.e. the raw/non-success path); success detection is substring `"SUCCEEDED"`.

**Requires a live mutation to establish (NOT captured, per the no-mutation rule):**
- The exact key set of each `playlistEditVideoAddedResultData`. The plugin *assumes*
  `videoId` + `setVideoId`; unverified.
- How a silently-dropped (region-locked/private) video is represented on the success
  path: `None`, `setVideoId: null`, missing element, or other.
- Exact duplicate behavior/shape for `duplicates=False` vs `True`.
- Whether per-item results always map 1:1 to the requested action order.

**This is the key open question for the adjudicator:** the current "assume the whole
request landed" fallback only triggers when the shape is entirely unexpected. If the
capture shows dropped items are represented as `None` or missing `setVideoId`, the
existing code already handles them; the decision is whether the unknown-shape fallback
should become a tri-state instead of claiming success.

### Minimal safe capture procedure (not run; needs explicit approval)

Guarded like `scripts/smoke.sh --mutating`; fully reversible; never touches an existing
playlist. If the premium session decides the per-item shape must be known, this is how:

1. Throwaway playlist `ZZ-CAPTURE-DELETE-ME-$$`, `privacy_status="UNLISTED"`, wrapped in
   `try/finally`.
2. Tee the **raw** JSON for `endpoint == "browse/edit_playlist"` by wrapping
   `YTMusic._send_request` before calling `add_playlist_items`.
3. Capture three cases: all-accepted; dropped (private/region-locked id); duplicate
   (with `duplicates=False` and `True`).
4. `finally: delete_playlist(pid)` and assert it is gone.
5. Save each raw dict as a `_confirmed_ids` unit-test fixture.

Known-good ids from `smoke.sh:67-68`: `HUskuj8I9xY`, `dQw4w9WgXcQ`.

---

## Item 5 — Quickshell `Process` lifecycle

### Signal surface

`/usr/lib/qt6/qml/Quickshell/Io/quickshell-io.qmltypes` and upstream `src/io/process.hpp:219-231`:

| Signal | Notes |
|---|---|
| `started()` | `process.hpp:220` |
| `exited(int exitCode, QProcess::ExitStatus)` | `process.hpp:221` |
| `runningChanged()` | `process.hpp:223` |
| `processIdChanged()` | `process.hpp:224` |

**There is no QML-visible error signal.** `onErrorOccurred` is a **private slot**
(`process.hpp:236`), not exported. `QProcess::ProcessError` cannot be observed from QML.

Built-in props confirmed: `running`, `processId`, `command`, `stdout`/`stderr` parsers.

### Emission order (`src/io/process.cpp`)

- **Normal start** (`onStarted`, `:268-272`): `processIdChanged` → **`runningChanged`**
  (false→true) → **`started`**. Note `runningChanged` fires *before* `started`.
- **Normal finish** (`onFinished`, `:274-287`): `process = nullptr` → parsers end →
  **`exited`** → **`runningChanged`** (true→false) → `processIdChanged` →
  `startProcessIfReady()`. Note `exited` fires *before* `runningChanged`.
- **`FailedToStart`** (`onErrorOccurred`, `:289-297`): `qWarning` → `process`
  nulled → **`runningChanged` only**. `started` does **not** fire, `exited` does **not**
  fire, `processIdChanged` does **not** fire.

**Confirms the bug:** a missing/non-executable `ctlPath` bypasses both `onStarted`
(deadline never armed) and `onExited` (busy never cleared). The **only** observable
event is `runningChanged`, with `running === false`.

### Restart semantics (`setRunning` / `startProcessIfReady`, `process.cpp:45-49,180-217`)

- `running = true` while already running: **no-op now, but `targetRunning` stays true**;
  on the current process's finish, `startProcessIfReady()` starts it again. Effectively
  **queues exactly one restart** (matches the Phase 1 buffer-mixing rationale).
- `running = false`: sets `targetRunning = false`; if running, `terminate()` (SIGTERM);
  `runningChanged` fires later on finish.
- A process cannot start before the post-reload hook (`reload.hpp:122-141`).

### Fix implication (decision left to the adjudicator)

Because `onStarted` arms the deadline and `onExited` clears `busy`, either:
- (a) key on `onRunningChanged` (`if (!running) …`) / pre-validate `ctlPath`
  executability, or
- (b) move deadline arming out of `onStarted` into a `startProcess()`-level watchdog.

Caution: `runningChanged` also fires on normal finish (after `exited`) and during a
queued restart, so a naive `if (!running) clear busy` could clear `busy` for an
operation whose restart is still pending. Whether any `busy`-setting process can queue
a restart is the thing to check.

### Unresolved

Upstream does not document `onRunningChanged` as the supported failure hook. All
`FailedToStart` causes funnel through `process.cpp:290-296`, so the hook is complete,
but it is not an officially blessed API.

---

## Item 2 — daemon attaches to any user-owned `mpv.sock`

### Attach path

- `private_mpv_socket()` (`:494-501`) checks only: runtime dir is a real 0700 uid-owned
  dir, and `MPV_SOCKET` is a uid-owned socket. **No pid/executable check.**
- `monitor_mpv_events()` (`:4079`) connects at **`:4168`** (`sock.connect(MPV_SOCKET)`)
  after only the socket-ownership admission. No call to `load_mpv_pid()` or
  `mpv_process_identity()` at or after connect.
- After connecting it sends `observe_property` for all `OBSERVED_PROPERTIES`
  (`:4169-4171`), calls `flush()` (`:4173`), then streams. `flush()` writes status,
  schedules precache, and notifies track changes. The `insert-next` is issued later by
  `install_cached_next` (`~:3135-3138`).

### Reusable identity predicate

`mpv_kill()` (`:941-994`) already performs the check at `:948-957`:
```
pid_data = load_mpv_pid() or {}
pid = pid_data.get("pid")
identity = mpv_process_identity(pid) if isinstance(pid, int) and pid > 1 else None
expected_executable = os.path.realpath(shutil.which("mpv") or "")
identity_matches = (
    identity is not None
    and identity[0] == pid_data.get("start_time")
    and identity[1] == pid_data.get("executable") == expected_executable
    and pid_data.get("socket") == MPV_SOCKET
)
```
Fields compared: `pid`, `start_time`, `executable` (must equal both the pidfile value
and `realpath(which("mpv"))`), `socket == MPV_SOCKET`.

Pidfile (`MPV_PID_PATH`, `mpv_pid_record()`) is written immediately after each
`subprocess.Popen` mpv spawn at `:1047, :2503, :3305, :3414, :3480, :3554`, but the
daemon polls concurrently and never consults it — so there is **no ordering enforcement**.

### Legitimate no-pidfile case (must be decided)

`MPV_SOCKET` is a fixed path. A user who manually runs
`mpv --input-ipc-server=$XDG_RUNTIME_DIR/yt-music/mpv.sock <urls>` creates a user-owned
socket in the private dir with **no pidfile**. Today the daemon attaches and mirrors it.
Gating on the identity predicate would stop that. The adjudicator must choose:
- gate hard on identity (foreign/manual mpv is ignored), or
- attach read-only (mirror status) but refuse queue mutation (`insert-next`) without a
  matching identity.

Also note `mpv_kill` (`:946-947`) quits any pre-existing private-socket instance before
its identity check — a separate asymmetry.

---

## Item 4 — 100-track limit

### `limit=None` verified as a full fetch

- Signature `mixins/playlists.py:18-20`: `get_playlist(self, playlistId, limit: int | None = 100, ...)`.
- Continuation loop `continuations.py:48`: `while continuation_token and (limit is None or len(items) < limit):`
  → with `limit=None` it runs until no token remains (one HTTP `browse` per page).
- Called from `mixins/playlists.py:227-230`; OLA branch `parsers/playlists.py:96-120`;
  `get_liked_songs` forwards `limit` the same way.

So `limit=None` is correct and complete, at the cost of N sequential round-trips and a
large parsed/payload/cache footprint per call.

### All call sites and classification

| # | Line | Function | Classification |
|---|---|---|---|
| 1 | 1444 | `_add_video_ids` | **full required** — duplicate detection |
| 2 | 1516 | `cmd_playlist_add_items` | **full required** — `p:<playlistId>` source resolution |
| 3 | 1609 | `cmd_playlist_move` | **full required** — index → setVideoId |
| 4 | 1743 | `cmd_dislike` (?) | **full required** — must find track in every owned playlist |
| 5 | 1852 | `cmd_playlist_tracks` | **display only** — can stay paged |
| 6 | 1896 | `cmd_remove` | **full required** — videoId → setVideoId |
| 7 | 3384 | `cmd_queue_playlist` | **full required** — "play the whole playlist" |
| 8 | 3449 | `cmd_enqueue` | **full required** — "play/queue/next the whole playlist" |

Note: the enclosing function names for #4/#5/#6/#7/#8 were inferred by the evidence
agent from context and should be re-confirmed by reading the function headers before
editing (line numbers themselves are verified).

### Performance note

Liked Songs is the worst realistic case (potentially thousands of tracks): `limit=None`
means tens of round-trips and a large JSON/cache payload on every cold or `-r` call.
The mutation sites genuinely need it; consider (a) caching the full list for the
duration of a single command, (b) an explicit cap with a clear error rather than a
silent wrong-index edit, and (c) leaving the display call at a page size.

---

## Consolidated open questions for the premium adjudicator

1. **Item 1:** on-disk positive-validity marker with TTL (and what TTL), vs. lazy
   validation, vs. retry-on-empty? Must preserve the offline stale fallback.
2. **Item 2:** hard identity gate vs. read-only attach for the no-pidfile manual-mpv
   case; and whether to log the refusal (daemon currently swallows exceptions).
3. **Item 3:** is a live mutation capture worth it, or should the fallback become a
   conservative tri-state and re-read the playlist after edits? (Note: `_confirmed_ids`
   already handles both known shapes; the unknown is only the per-item field set.)
4. **Item 4:** `limit=None` at the seven correctness sites; cap-and-error vs. full fetch
   for very large playlists; keep `:1852` paged.
5. **Item 5:** `onRunningChanged` hook vs. `startProcess()`-level watchdog vs. a
   pre-flight executability check on `ctlPath`.
