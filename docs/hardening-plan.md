# Hardening plan (2026-10-06)

The player's feature surface is complete; this plan targets reliability and
maintainability rather than new features. Ordered by leverage and risk. Each
phase is independently shippable and must leave the plugin working.

Guiding rules:
- No behaviour change unless a phase says so.
- Every phase keeps `node scripts/model_test.js` and the offline `smoke.sh`
  sections passing, and adds coverage for what it changes.
- Prefer moving pure logic out of QML into `Model.js` (testable in Node) over
  adding more code to `Panel.qml`.

## Phase 0 — Measure and guard (done as this plan is written)
- Count the surface: `Panel.qml` 6,158 lines, 130 functions, 54 `Process`, and
  ~40 deadline `Timer`s; backend 5,898 lines; `Model.js` 449 lines.
- Establish the test baseline: 163 `model_test.js` assertions, N offline smoke
  sections. These become the regression gate for every later phase.

## Phase 1 — Pure-logic extraction into Model.js (LOW RISK)
Move logic that is pure (no QML object references) out of `Panel.qml` so it can
be unit-tested in Node instead of only by hand. Candidates, in order:
1. `normalizeSong` / `normalizeSongs` / `normalizeMixedRows` / `normalizeStations`
   / `normalizePlaylists` — row shaping; currently tangled with `boundedString`.
2. `songSubtitle` / `stationSubtitle` / `librarySongCount` / `songCount`.
3. `queueUpcomingCount` / `queueCurrentIndex` — already pure math.
4. URL/arg helpers (`isVideoId` is already a good citizen).
Rationale: these are the most likely to regress and the cheapest to test. This
phase only *moves* code and adds `model_test.js` assertions; the QML calls the
new `Model.*` names.

### Progress
- [x] `boundedString`, `isVideoId`, `normalizeSong`, `normalizeSongs`,
  `normalizeMixedRows`, `normalizeStations`, `normalizePlaylists` moved to
  `Model.js` and exported; `Panel.qml` now has one-line wrappers. `model_test.js`
  grew from 163 to 190 assertions covering them (invalid ids, duration clamps,
  non-array tolerance, tag cleaning, id-less playlist drop).
- [x] `songSubtitle`, `stationSubtitle`, `songCount`, `queueUpcomingCount`,
  `queueCurrentIndex` moved to `Model.js` (as pure functions taking rows +
  position); `Panel.qml` keeps thin wrappers that supply root state.
  `model_test.js` grew to 210 assertions.
- [ ] `librarySongCount` stays a trivial root wrapper (`songCount(libraryRows)`).
- Phase 1 complete. Next: Phase 2 (process/state-machine hardening).

## Phase 2 — Process/state-machine hardening (MEDIUM RISK)
The 54-process + ~40-timer mesh is where our real bugs came from (stuck flags,
stale responses, double fetches). 
1. Audit every `Process` against the same checklist used in the review passes:
   stdout+stderr parsers, `onStarted`/`onExited`, a deadline, flags cleared on
   both success and timeout, and a request token where a late response could
   clobber newer state (library/tracks/search already do; stations/queue do
   not uniformly).
2. Centralise the request-token pattern: one helper on the root
   (`beginRequest(key)` -> monotonic token; `isCurrent(key, token)`) replacing
   the ad-hoc `libraryRequestSeq` and the `sent === root.stationQuery` idiom.
3. Add an offline test seam: a small headless harness that loads `Model.js` and
   drives the extracted state machines (from Phase 1) so the token/flag logic
   is covered without a live shell.

### Progress
- [x] Added `AsyncState.js` with three tested factories: `makeTokenSource`
  (monotonic tokens + `invalidate`/`isCurrent`), `makeDirtyFlag`
  (`mark`/`take`/`clear`), `makePendingQueue` (`offer`/`take`/`clear`, with
  `null`/`undefined` coerced to ""). `scripts/asyncstate_test.js` covers them
  (26 assertions).
- [x] Wired them into `Panel.qml`, replacing the ad-hoc `tracksRequestSeq`,
  `libraryRequestSeq`, `stationFavoritesDirty`, `stationCatalogDirty`,
  `pendingStationSearch` and `pendingSearch` bookkeeping with the shared
  objects. Behaviour-preserving; `qmllint` clean.
- [ ] Extend the token pattern to the remaining fetch processes that use
  ad-hoc staleness guards (`searchProc` query compare, `albumStatusProc`
  refId compare) and give every fetch process a deadline if it lacks one.
  Decision: left as-is. `searchProc`'s `data.query === root.searchQuery` and
  `albumStatusProc`'s `refId === libraryRefId` are already correct staleness
  guards (and the search one also drives the UI); replacing them with tokens
  would be churn for no behavioural gain.
- [x] Audited the timeout timers: all 28 fetch processes have a deadline, and
  every flagged state has a clearing path on timeout. The only processes
  without a deadline are the fire-and-forget writers (`cavaWriteProc`,
  `cavaProc`, `uiSaveProc`, `uiLoadProc`), which is correct.
- [x] Added `scripts/asyncstate_harness.js` (42 assertions): drives the token/
  dirty/pending scenarios headlessly and asserts the panel's source shape
  (no ad-hoc bookkeeping names remain; every fetch process exists; a deadline
  per fetch).

## Phase 3 — Break up Panel.qml (MEDIUM RISK, largest diff)
Extract self-contained regions into components, one per commit, verifying after
each:
1. Visualizer renderers (`spectrumRenderer`/`vuRenderer`/`vuv`/`vus`/`dots`) ->
   `VisualizerStrip.qml`. Pure view, feeds on properties.
2. Stations tab (sections, search, rows, context menu wiring) ->
   `StationsTab.qml`.
3. Context-menu builders (`rebuildContextMenu`/`rebuildStationMenu`/
   `rebuildPlaylistPicker`) -> a small `Menus.js` or a `ContextMenus.qml`.
Target: `Panel.qml` under ~4,000 lines. Do not attempt the Repeater->ListView
virtualisation (already declined; high regression risk for low payoff).

## Phase 4 — Backlog reliability items (LOW RISK, user-visible)
Close the confirmed-but-unfixed items in `docs/backlog.md`:
- Station accepted-but-silent still reports success (add a progress/metadata
  check).
- Optimistic favourite star (flip immediately, revert on failure).
- `Model.cavaScaleBars` Infinity guard.
- Unexpected mpv death: reconcile the live marker instead of leaving it stale.
- Radio History section, LIVE badge on the pinned row, and cava `source`
  pinning to the monitor.

### Progress
- [x] Silent station: `wait_for_stream_progress()` added; the cold-start path
  fails a stream that neither advances its position nor yields real ICY
  metadata. Offline smoke covers dead/progressing/ICY.
- [x] Optimistic favourite star (`stationFavOverrides`, revert on failure).
- [x] `cavaScaleBars` Infinity guard + `model_test.js` coverage.
- [x] Stale live marker reconciled in `cmd_status`.
- [ ] UI gaps remain (not reliability): Radio History section, LIVE badge on the
  pinned queue row, and pinning cava's `source` to the output monitor.

## Phase 5 — Dependency and resilience checks (LOW RISK)
- `install.sh` / `doctor`: report cava presence (done) and add a dependency
  self-check summary.
- Confirm the panel degrades cleanly when `yt-music-ctl`, `mpv`, or `cava` is
  missing, and when `status.json` is absent/corrupt.

## Phase 6 — Discoverability (LOW RISK)
- A keyboard-shortcut / context-menu cheat sheet in the panel, and a first-run
  hint covering the right-click menus (station rows, queue rows, bar icon).

## Sequencing
Phases 1 -> 2 -> 4 -> 5 -> 6 are low/medium risk and can ship incrementally.
Phase 3 is the largest and goes last, one component per commit. Phase 4 items
are independent and can be interleaved whenever a break from refactoring helps.
