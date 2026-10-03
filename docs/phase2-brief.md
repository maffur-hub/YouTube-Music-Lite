# Phase 2 brief — the five "needs design" fixes

Status: prepared 2026-10-03. Not started. Companion to `docs/review-findings.md`.

## Objective

Resolve the five correctness-sensitive findings that were deliberately deferred from
Phase 1. Each needs a design decision before editing, because a naive fix trades one
bug for another (auth caching can break offline fallback; socket checks can break
legitimate attach; pagination can make large playlists slow).

## Ground rules (apply to every item)

- Repo: `/home/matt/.config/omarchy/plugins/io.github.maffur-hub.youtube-music-bar`.
  The installed `yt-music-ctl` symlinks `~/.local/share/yt-music/yt_music.py` to
  `backend/yt_music.py`, so edits are live for `scripts/smoke.sh`.
- Minimal diffs. Preserve every public CLI command, its JSON shape, and its exit code.
- Keep the existing security posture: `lstat` + `O_NOFOLLOW` + pid identity checks;
  never widen a trust boundary to simplify a fix.
- Do not break the offline/stale cache fallback (`_serve_stale`, `_ytmusic_for_cache`).
- Do not commit. Leave all changes for review.
- Each item that can be tested offline gets a regression test in `scripts/smoke.sh`
  (see the existing "review regressions (offline)" section at the end).
- Gate: `python3 -m py_compile backend/yt_music.py`, `/usr/lib/qt6/bin/qmllint Panel.qml`
  (QML items), and `scripts/smoke.sh` → `FAIL 0`.

## Recommended workflow (adjudicator pattern)

Keep implementation on the cheap model; rent the premium model only for step 3.

1. **Evidence** (cheap): read the code *and* the installed library/Quickshell source.
   For empirical questions (real API response shapes, `Process` signal order) capture
   evidence — do not trust recollection.
2. **Propose** (cheap): per item, 1–2 candidate designs with failure modes and the
   regression test that would catch each failure.
3. **Decide** (premium, only items 1/2/3/5): pick one, record why the other was
   rejected. Item 4 is empirical and needs no premium input (see below).
4. **Implement** (cheap) + add the test.
5. **Verify** (cheap + orchestrator): smoke, compile/lint, final diff review.

---

## Item 1 — `validate_auth()` runs a live network call on every authenticated command

**Where.** `get_ytmusic()` (`backend/yt_music.py:803`) calls `validate_auth(auth)`
(`:895`), which writes a temp `.auth-test.json` into `CONFIG_DIR`, constructs a
`YTMusic` client, and calls `get_account_info()` — a network round-trip — before any
real work. Every authenticated command pays this, and it makes the stale-cache
fallback mostly moot because the auth bootstrap fails first when offline.

**Why it exists.** YouTube returns an *anonymous* library page instead of an error when
cookies expire, which is indistinguishable from an empty account. The pre-check is how
the code tells "expired" from "empty".

**Design constraints.**
- There is no cross-process memory: each CLI invocation is a new process, so any cache
  must be on disk with a short TTL.
- Caching "valid" must not mask a session that expires inside the TTL; caching
  "invalid" must not lock the user out after re-login.
- Must still refresh from browser cookies (`build_browser_auth`) when validation fails.

**Candidate directions (pick one in step 3).**
- (a) On-disk positive-validity marker in `STATE_DIR` (e.g. `auth-valid.json`, 0600)
  with a TTL; skip the network check while fresh, force it on `-r` / login and on any
  command that fails auth.
- (b) Drop the pre-check and treat an empty library + present cookies as "verify now",
  accepting one retry.
- (c) Validate lazily only for commands that read account-scoped data.

**Acceptance.**
- A warm command performs zero `get_account_info()` calls within the TTL.
- With the network down and a cached payload, the offline fallback still serves stale
  data (this is the test that must not regress).
- The first command after TTL expiry / `-r` revalidates and, on failure, refreshes
  from browser cookies or fails with the existing "session expired" message.

**Regression test (offline-testable part).** Age the cache, point `HTTPS_PROXY` at a
dead port, run an authenticated read, and assert the stale payload is still served
(extend the existing `check_stale` pattern). Measure/count is hard in bash; prefer a
targeted Python assertion over the module.

---

## Item 2 — daemon attaches to any user-owned `mpv.sock`

**Where.** `monitor_mpv_events()` (`backend/yt_music.py:4079`) connects to
`MPV_SOCKET` after `private_mpv_socket()` (`:494`), which only checks "runtime dir is
0700, socket is a socket and owned by uid". It never checks that the mpv behind the
socket is the plugin's. A foreign/stale mpv squatting the path makes the daemon observe
foreign events and issue `loadfile … insert-next` into that mpv's queue.

**Existing machinery to reuse.** `mpv_kill()` (`:941`) already does the identity check:
`load_mpv_pid()` (`:505`) + `mpv_process_identity(pid)` compared against
`start_time`, `executable`, and `pid_data["socket"] == MPV_SOCKET`. Extract a helper
(e.g. `managed_mpv_identity_matches()`) and require it before observing.

**Design constraints.**
- Must not break the legitimate case where the plugin's own detached mpv is the one
  on the socket (pidfile written at launch, `:1047`, `:2503`, …).
- Decide the failure mode on mismatch: do not attach at all (daemon idles) vs. attach
  read-only without queue mutation. Prefer "do not attach" + a log line.
- The daemon currently swallows exceptions silently (`:4192`); any new refusal must be
  observable (log to `DAEMON_LOG`), otherwise this is undebuggable.

**Acceptance.**
- With a pidfile that does not match the socket owner, the daemon does not observe or
  write status/mutate the queue.
- With a matching plugin-launched mpv, behavior is unchanged.

**Regression test.** Python-level: build a temp runtime dir, a fake pidfile, and assert
the new helper returns False for a mismatched `start_time`/`executable`/`socket` and
True for a match. No real mpv needed.

---

## Item 3 — `_confirmed_ids()` wrong on partial responses

**Where.** `_confirmed_ids(response, requested)` (`backend/yt_music.py:1381`) feeds
`_add_video_ids()` (`:1431`). It currently assumes the whole request landed when
`playlistEditResults` is missing or not a list, and marks everything not carrying a
`setVideoId` as skipped.

**Why this is empirical, not a reasoning problem.** What YouTube actually returns for
(a) all accepted, (b) a region-locked/private video silently dropped, and (c) a
duplicate, in `ytmusicapi` 1.12.2, is the whole question. Capture it.

**Evidence step.** Add a temporary instrumented run (or a small script against a
throwaway playlist, guarded like `smoke.sh --mutating`) that dumps the raw
`playlistEditResults` for those three cases. Store the captured shapes as fixtures.

**Candidate directions.**
- (a) Derive `confirmed` strictly from per-item results, and when the shape is
  unrecognised return a tri-state ("unknown") rather than claiming success; report
  conservatively.
- (b) Ignore edit results entirely and re-read the playlist after the edit to compute
  what is actually present (authoritative, one extra request).

**Acceptance.**
- No case where a non-empty request is reported as fully skipped when it partially
  landed, and none where a partially-landed request is reported fully added.
- The `added / duplicates / skipped / skipped_ids` contract printed by
  `playlist-add-items` stays the same shape.

**Regression test.** Unit-test `_confirmed_ids` against the captured fixtures.

---

## Item 4 — playlist ops only see the first 100 tracks

**Where.** `get_playlist(playlist_id, limit=100)` at `backend/yt_music.py:1444`
(duplicate detection), `:1516` (token→index resolution), `:1609` (view), `:1743`
(move), `:1852` / `:1896` (remove by index), `:3384` / `:3449` (queue/saved-queue).

**Empirical fact already established.** `ytmusicapi` 1.12.2 `get_playlist` signature is
`(playlistId, limit=None, related=False, suggestions_limit=0)` and the docstring states
**`limit=None` retrieves them all**. So the correctness fix is available without
continuation code.

**Design decision (no premium needed).** Audit each of the eight sites:
- Full list *required* for correctness: duplicate detection (`:1444`), move (`:1743`),
  remove-by-index (`:1852`, `:1896`), token resolution (`:1516`).
- Display-only (can stay paged): playlist view (`:1609`), queue (`:3384`, `:3449`)
  unless the UI exposes operations that need stable global indices.

**Constraint.** `limit=None` on a very large playlist costs a long series of requests.
That is acceptable for edit operations (which need correctness) but should not be used
for a display fetch. If an edit on a huge playlist becomes slow, cap and report rather
than silently edit the wrong index.

**Acceptance.**
- A playlist with >100 tracks: adding an existing track is reported as a duplicate;
  removing/moving an entry past index 100 acts on the correct track.
- Display commands keep their current latency profile.

**Regression test.** Python-level: call the site-specific helper with a fixture of 150
tracks and assert indices past 100 resolve correctly. (Real end-to-end needs a large
throwaway playlist and belongs behind `--mutating`.)

---

## Item 5 — a missing `yt-music-ctl` leaves `busy`/`refreshing` stuck forever

**Where.** `Panel.qml`: every timeout timer is armed in `onStarted` (e.g. `:1252`,
`:1297`, `:1550`) and every `busy`/`refreshing` reset is in `onExited` (`:~1258`,
`~1339`, …). Quickshell's `Process` does not emit `started`/`exited` on
`QProcess::FailedToStart`, so if `~/.local/bin/yt-music-ctl` is missing or not
executable, `busy` never clears and every action answers "Still finishing…".

**Evidence step.** Confirm the signal order from the installed Quickshell source
(`src/io/process.cpp`) before designing: which signals fire on `FailedToStart`, and
whether `onRunningChanged` fires on a normal completion (to avoid clearing `busy`
before `onExited` runs).

**Candidate directions.**
- (a) Arm a watchdog independent of `onStarted`: `startProcess()` starts a single
  timer; if the target process is not running when it fires, clear `busy`/`refreshing`
  and show an error. One change covers every process.
- (b) Add `onRunningChanged`/error handling per process that owns a `busy` flag.

Direction (a) is preferred if the signal order is safe; it fixes the whole class rather
than the symptom and is where premium judgment helps.

**Acceptance.**
- Rename/chmod the wrapper and trigger an action: the UI shows an error within
  `commandTimeout` and `busy` clears; the panel remains usable.
- Normal fast commands are unaffected (no double-clear, no flicker).

**Regression test.** Not bash-testable (needs the running shell). Add a manual
click-through checklist item; verify `qmllint` stays clean.

---

## Budget and model routing

- Run steps 1, 2, 4, 5 on the cheap model (DeepSeek V4.1 Flash) as in Phase 1.
- Premium (GLM-5.3) only for step 3 on items 1, 2, 3, 5 — a single focused session
  reading the code and the candidate designs, not the whole repo. Expected < $1, well
  under the $3 / 5-hour and $15 / month premium caps.
- Item 4 skips step 3: the `limit=None` fact is already established.

## Definition of done

- All five items either fixed with a design note, or explicitly deferred with a reason.
- `scripts/smoke.sh` → `FAIL 0`, plus the new offline regression checks.
- `docs/review-findings.md` needs-design boxes updated.
- Diffs left uncommitted for review.
