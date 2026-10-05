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

- **A station URL that mpv *accepts* but never plays is still reported as
  successful.** `cmd_station_play` now fails a switch when `playlist-count`
  never grows (mpv rejected the URL), but a dead URL that is accepted and then
  fails to produce audio keeps mpv alive, so the command prints `ok:true` and
  records history. Detecting this reliably needs a playback-progress check.
- **The favourite star is not optimistic.** It only flips after the
  `station-fav-add`/`remove` round trip and the favourites refetch, so there is
  no immediate feedback while the request is in flight.
- **`Model.cavaScaleBars` is not `Infinity`-safe** (`Math.min(100, v * Infinity)`
  is `NaN`). Not reachable through the current pipeline (all factors are finite
  and the headroom divisor is guarded), but worth clamping if a caller changes.
- **An unexpected mpv death leaves the live marker on disk.** It is not
  reflected in status (the no-player branch returns early) and self-heals on the
  next play/stop/queue edit, but a status reader could briefly see a stale file.

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
