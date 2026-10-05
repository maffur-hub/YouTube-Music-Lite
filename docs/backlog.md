# Backlog and known feature gaps

Follow-ups recorded by the 2026-10 review passes. These are **not** bugs; they
are deliberate gaps or design risks to revisit. Delete entries once handled.

## Feature gaps

- **Radio History tab is not wired up.** The backend exposes
  `yt-music-ctl station-history [limit|clear]` and persists
  `~/.local/state/yt-music/radio-history.json` (deduped, capped at 50), but the
  Stations tab only renders Featured / Favorites / Search. Either add a fourth
  section or drop the command.
- **No explicit LIVE badge on the pinned Up Next row.** The Hero shows
  "LIVE RADIO" and the station list shows a "LIVE" badge, but the queue's
  stream row carries only a globe glyph. Commit 95ed42f's message promised a
  LIVE badge, so this is a completeness gap rather than a regression.
- **No station filters beyond name/genre.** `station-search` supports `--tag`
  and `--limit` only; there is no country/codec/bitrate filter in the UI or the
  command.

## Remaining (known, not yet fixed)

- **The favourite star is refreshed only after the authoritative list lands.**
  It is optimistic now, but the override is cleared wholesale when any
  favourites fetch completes; a slow concurrent fetch could briefly revert a
  just-toggled star.

## Fixed in the hardening pass (2026-10-06)

- [x] **A station URL that mpv accepts but never plays** is no longer reported
  as success. `wait_for_stream_progress()` requires the stream's position to
  advance or genuine ICY metadata to arrive (a title equal to the stream URL's
  own fragment does not count); a stream that never starts is killed and
  reported as a failure without touching history.
- [x] **`Model.cavaScaleBars` is `Infinity`-safe** — a non-finite scale is
  treated as the identity and a non-finite product clamps to 100, so a frame
  can never contain NaN heights.
- [x] **An unexpected mpv death no longer leaves the live marker on disk.**
  `cmd_status` reconciles `radio-current.json` in both no-player branches.
- [x] **The favourite star is optimistic** — it flips on click, reverts (and
  refreshes) on a reported failure, and is cleared once the authoritative
  favourites list reloads.

## Known risks / assumptions

- **The panel assumes Quickshell emits `onExited` when a deadline timer sets
  `Process.running = false`.** The `stationSearching` and
  `stationFavoritesDirty` flags (and every pre-existing process, e.g. library
  and playlist) depend on it. If that ever changes, a timed-out station search
  or favourites fetch would stay flagged as in-flight.
- **cava's `source = auto` may capture the default source, not the sink
  monitor.** The visualizer comment claims it reads the output monitor, but the
  generated config uses `method = pulse`, `source = auto`. On some PulseAudio
  setups that resolves to the microphone; pinning a `@DEFAULT_MONITOR@` source
  would make the intent explicit. This behaviour predates the visualizer
  commit (the original shipped `cava.conf` used the same input block).
- **cava `scaling` is version-dependent.** The panel now applies the
  linear/decibel curve itself because cava 0.10.7 (and 1.0.0) do not implement
  the `scaling` key. If a future cava starts honouring it, the generated config
  must not add the key back or the curve would be applied twice.
