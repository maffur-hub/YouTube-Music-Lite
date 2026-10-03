# Phase 2 design decisions

Adjudicated 2026-10-03 against `docs/phase2-brief.md` + `docs/phase2-evidence.md`.
Decisions only. Item 4 confirmed. No live mutation is required for any item.

## Item 1 — auth revalidation: on-disk positive marker with TTL

**Chosen.** Write `STATE_DIR/auth-valid.json` (via `json_dump`, 0600, containing only
`{"ts": ...}` — no secrets) after every *successful* `validate_auth`. While the marker
is younger than **12 h**, `get_ytmusic()` skips the `validate_auth` network call (and
only that — `refresh_auth_headers` and the auth.json write stay as-is). Force full
revalidation on `login`, on `-r`/`--refresh` of any metadata command, and — one-shot,
as a backstop — when the `playlists` listing returns empty while a marker exists
(revalidate + browser-cookie refresh + one retry).

**Rejected.** (b) lazy validate-on-empty alone: pays a second full command round-trip
on the hot path and makes the anonymous-empty ambiguity the primary mechanism instead
of a backstop. (c) per-command scoping: a broad audit for marginal gain over one
mechanism.

**Failure modes.**
- Cookie expiry inside the TTL → wrong "empty" answers for account reads for up to
  12 h; closed by the playlists-empty backstop and the TTL.
- Offline with a *fresh* marker: the data call fails and the command-level
  `_serve_stale` fallback serves stale — preserved, and faster than today (no double
  `get_account_info` timeout, no `browser_cookie3` decrypt before serving). Offline
  with a *stale* marker: today's `_ytmusic_for_cache` path serves stale. Both intact.
- Marker spoofing gains nothing: it is non-secret, 0600 in the already-0700
  `STATE_DIR`, and only *skips* a check.
- CLI JSON contract unchanged.

**Acceptance (offline, in the smoke "review regressions" style, module-import based).**
1. Fresh marker → monkeypatched `validate_auth` counter is 0 across `get_ytmusic`.
2. Stale marker → counter is 1 and the marker is rewritten.
3. `-r` forces revalidation regardless of marker age.
4. Must-not-regress: aged cache + dead proxy + *fresh* marker → stale payload still
   served.

## Item 2 — daemon mpv.sock identity: hard gate, reusing mpv_kill's predicate

**Chosen.** Extract `mpv_kill`'s check (`:948–957`) into
`managed_mpv_identity_matches()` (pid > 1, `start_time`, `executable` ==
`realpath(which("mpv"))`, `socket == MPV_SOCKET`). `monitor_mpv_events()` requires it
before `sock.connect` (`:4168`). On missing pidfile or mismatch: do not attach, mutate
nothing, and log one line to `DAEMON_LOG` only when the refused socket's
(inode, mtime) signature changes (no 0.5 s spam). The daemon loop's next poll retries —
which also self-heals the spawn race where the pidfile lands just after the socket.

**Rejected.** Read-only attach (mirror but refuse `insert-next`): creates a third
behavior class and needs gating deep inside the exception-swallowing precache worker
(`install_cached_next`, `~:3135`) — exactly the code we cannot observe today — to
preserve an undocumented manual-mpv case.

**Failure modes.**
- Plugin-spawn race (socket up, pidfile not yet written) → refused this attempt,
  retried ≤ 0.5 s later. Transient.
- mpv launched via a non-PATH executable → `realpath` mismatch → refused. Consistent
  with `mpv_kill`, which already uses the same comparison.
- Binary replaced on disk after spawn → `/proc/<pid>/exe` gains `(deleted)` → refused
  until the next spawn. Rare; fails closed.
- Manual mpv on the plugin socket (undocumented) is no longer mirrored — accepted
  behavior change; one line in CHANGELOG/README.
- No interaction with the offline fallback or the CLI JSON contract. Security posture
  strictly narrows: same `lstat`/`O_NOFOLLOW`/0600 pidfile reads, no new paths.

**Acceptance (offline, module-import based).**
1. Predicate truth table: match → True; mismatched `start_time` / `executable` /
   `socket`; `pid ≤ 1`; missing pidfile → all False.
2. `monitor_mpv_events` with `get_mpv_props` stubbed truthy and `load_mpv_pid`
   stubbed None returns *without* attempting a connection (assert via a monkeypatched
   `socket.socket`).

## Item 3 — `_confirmed_ids`: conservative hybrid; NO live capture required

**Chosen.** Turn `_confirmed_ids` into a shape classifier: recognized success-parsed
shape (unwrapped items with `videoId`+`setVideoId`) and recognized raw-wrapped shape →
confirmed as today; *anything else* → an explicit **"unknown"** sentinel instead of
`list(requested)`. In `_add_video_ids`, on "unknown": re-read the playlist
(`get_playlist(limit=None)` per item 4) and compute `added`/`skipped` from the
before/after diff (the "before" set already exists from duplicate detection at
`:1444`); if the re-read itself fails → conservative `ok:false` ("could not verify
additions") rather than claiming success.

**Live capture: not required.** The hybrid is robust to YouTube schema drift and needs
no account mutation. Capture would pin only today's shapes and go stale on the next
change.

**Rejected.** Keeping `list(requested)` on unknown shapes: a 200-OK error-ish raw
response without `playlistEditResults` currently reports everything as added — a
silent failure. Pure re-read-always: pays an extra fetch on every add for no benefit
when the shape is recognized.

**Failure modes.**
- YouTube browse-cache staleness on the re-read can *under*-report additions ("not
  added" when it was) → user retries → the duplicate pre-filter catches it.
  Deliberate direction: hiding a real failure is worse than a visible false negative.
- An add on a huge playlist now costs a second full fetch (with `:1444`) — accepted
  for an explicit mutation command.
- Concurrent edits during the diff window misattribute — single-user plugin.
- LM (Liked Music) path untouched (rating-based; never uses `_confirmed_ids`).
- CLI contract: the `(added, duplicates, skipped, duplicate_ids, skipped_ids)` tuple
  shape and exit codes unchanged; only the values become ground truth on unknown
  shapes. Mutation commands correctly have no stale-serve (never replay an uncertain
  edit). No security surface.

**Acceptance (offline; `_add_video_ids(ytm, ...)` takes `ytm` as a parameter, so a
fake ytm is injectable).**
1. Classifier unit tests with synthesized fixtures for the three input classes
   (shapes taken from the evidence pack's library source).
2. Fake-ytm flow: unknown shape + before/after playlists → reported `added` == diff;
   re-read raises → `ok:false`.

## Item 5 — stuck busy: request-time watchdog + instant pre-flight check

**Chosen.** Two simple mechanisms.
1. **Primary — request-time watchdog:** move deadline arming out of each Process's
   `onStarted` into `startProcess()` via a key→Timer map. On timeout: if
   `proc.running` → kill (today's semantics); else → clear that flow's flag and set a
   "backend unavailable" statusText. Fixes the whole class (every `FailedToStart`
   cause, not just a missing binary).
2. **Secondary — instant feedback:** an existence/executability check on
   `root.ctlPath` consulted in `startProcess()` (e.g. a `Quickshell.Io` `FileView`
   with `exists`), erroring immediately instead of waiting `commandTimeout`. Verify
   `FileView.exists` exists in the installed qmltypes; if not, ship the watchdog alone.

**Rejected.** Per-process `onRunningChanged` clear-handlers: `runningChanged(false)`
also fires on normal finish (harmless double-clear) *and* during a queued restart's
gap (`exited` → `runningChanged(false)` → restart), where clearing a flow flag while
the restart is still pending is a false-clear; and it duplicates a handler per process
instead of one map. Any design that leaves arming dependent on a signal that never
fires on `FailedToStart` is rejected outright.

**Failure modes.**
- Watchdog double-clear with `onExited` — idempotent, no-op.
- Queued-restart interaction: a restart that has not started for a full
  `commandTimeout` is a failure, so killing/clearing then is correct.
- `exists` true but wrapper broken (venv python gone) → still fails to start →
  watchdog catches it (defense in depth).
- One timer arm/stop per command — negligible.
- UI-only: no CLI contract, offline-fallback, or security interaction.

**Acceptance.** Not bash-testable (needs the live shell). qmllint clean, plus a
manual checklist: rename/chmod the ctl wrapper → trigger a transport action → error
within seconds (pre-flight) or ≤ `commandTimeout` (watchdog), `busy` clears, panel
usable; restore wrapper → next action works without a reload; rapid double-click a
playlist during a load (queued restart) → no stuck spinner, correct rows.

## Item 4 — pagination: CONFIRMED, with one flagged consequence

`get_playlist(limit=None)` at `:1444`, `:1516`, `:1609`, `:1743`, `:1896`; keep
`:1852` (display) paged. Two flags:

1. **The two playback/queue paths (`:3384`, `:3449`) change behavior on huge
   playlists** — today they queue at most 100 tracks; after the fix they queue the
   entire playlist. That matches the commands' documented intent, so default to
   `limit=None` there too, note it in the changelog, and accept that the UI list
   stays capped at 500. If the latency (tens of seconds on a multi-thousand-track
   playlist) proves unacceptable, the acceptable fallback is an explicit bounded cap
   with a visible "queued first N of M" message — never a silent cap.
2. The enclosing-function attributions for `:1743/:1852/:1896/:3384/:3449` were
   inferred from context — re-confirm the function headers before editing (the line
   numbers themselves are verified).

Also note the interaction with item 3: an add on a huge playlist pays two full
fetches (dup-detection + verification re-read). Accepted.

## Implementation order (for steps 4–5, cheap model)

4 → 3 → 2 → 1 → 5: start with the most mechanical and best-evidenced (pagination),
then the classifier+re-read, then the predicate extraction, then the marker, then the
QML watchdog last (only item that cannot be smoke-tested).

## Implementation status (2026-10-03)

- **Item 4 — DONE.** `limit=None` at the 7 correctness sites; `cmd_playlist_tracks`
  (`:1853`) kept at `limit=100`. Offline regression added in `smoke.sh`
  ("full-page playlist fetch"): asserts exactly one paged `get_playlist` remains and
  that duplicate detection finds a track beyond index 100. `smoke.sh` → `PASS 51 / FAIL 0`.
- **Item 5 — DONE (statically).** `startProcess()` arms the deadline via
  `deadlineFor(key)`; the eight flag-owning timers (status/search/play/mix/queue/
  logout/create/cmd) plus `lyricsDeadline` clear their flag via `commandTimeoutHit(key)`
  when the process is not running. `FileView` has no `exists`, so the pre-flight check
  was dropped per the decision. `qmllint` clean. The runtime checklist (rename the
  wrapper, confirm `busy` clears within `commandTimeout`) still needs a live shell.
- **Items 1, 2, 3 — DONE.**
  - Item 3: `_confirmed_ids` is a strict classifier returning `None` for unknown
    shapes; `_add_video_ids` re-reads the playlist (`limit=None`) and computes
    added/skipped from the before/after diff, raising a clear error if the re-read
    fails (conservative `ok:false`). No live capture used.
  - Item 2: `managed_mpv_identity_matches()` extracted from `mpv_kill` and required
    at the top of `monitor_mpv_events`; refusals are logged to `DAEMON_LOG` once per
    distinct socket.
  - Item 1: `STATE_DIR/auth-valid.json` marker with a 12 h TTL; `get_ytmusic` skips
    `validate_auth` while fresh; `-r`/login force revalidation; `cmd_playlists`
    revalidates once on an empty list under a fresh marker.
- All five items are covered by offline regression sections in `scripts/smoke.sh`
  (`review regressions`, `full-page playlist fetch`, `playlist edit result
  classification`, `managed mpv identity gate`, `auth-validity marker`).
  `smoke.sh` -> `PASS 54 / FAIL 0`; `qmllint Panel.qml` clean.

