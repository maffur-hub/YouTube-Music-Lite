# Changelog

## Unreleased

Review-driven fixes (see `docs/review-findings.md`, `docs/phase2-decisions.md`).

- Hardening: `doctor` now reports `cava` and `python3` too, and the panel no
  longer leaves a stuck spinner/flag when `yt-music-ctl` is missing (the
  station-search, station-play, playlist and library deadlines now clear their
  state even when the process fails to start). A missing featured-station
  deadline was also fixed.
- A radio station that mpv accepts but never actually plays is no longer
  reported as a success (and no longer recorded in history): playback is only
  confirmed once the stream position advances or real ICY metadata arrives.
- The favourite star in the Stations tab now flips immediately on click and
  reverts if the update fails, instead of waiting for the round trip.
- An unexpected mpv death no longer leaves a stale "live radio" marker behind;
  `status` reconciles it in the no-player branches.
- The visualizer is safe against a non-finite scale factor (no more NaN bar
  heights).

- The chosen volume is now remembered across mpv respawns. Every playback start
  (play, radio, restore, resume, mix, enqueue) spawns a fresh mpv, which always
  began at 100%, so the volume jumped to full after stopping or switching; the
  level is now persisted (and picked up from media-key/MPRIS changes) and passed
  to each spawn as `--volume`.
- Hardening: moved pure row-shaping helpers (`boundedString`, `isVideoId`,
  `normalizeSong`/`normalizeSongs`/`normalizeMixedRows`/`normalizeStations`/
  `normalizePlaylists`) from `Panel.qml` into `Model.js`, where `model_test.js`
  covers them (190 assertions). See `docs/hardening-plan.md`.

- The bar widget's right-click now opens a transport menu (Play/Pause, Previous,
  Next, Stop, Open/Close player) instead of stopping or refreshing playback;
  middle-click still toggles play/pause.

- Featured stations are now editable from the panel: right-click any station row
  (Featured, Favorites, or Search) to add it to or remove it from Featured. Adds
  and removes are coalesced with an in-flight catalog fetch so the list cannot
  stay stale.

- Fixed `yt-music-ctl volume` printing a traceback on non-numeric input; it now
  fails with a usage error like the other commands.
- Hardened `~/.config/yt-music/` and `auth.json` to 0700/0600 (they hold browser
  session cookies), matching the state/cache directories.
- Metadata cache refresh sentinels are now per-query instead of one global lock,
  so a stale screen no longer suppresses refreshes for other screens.
- Playlist edits and lookups now fetch the whole playlist (`limit=None`) instead
  of the first 100 tracks, so duplicates are detected and tracks beyond #100 can
  be removed or reordered. Queueing a very large playlist now queues all of it
  (previously capped at 100).
- Added a 12-hour on-disk auth-validity marker so most commands skip the live
  account-info validation round-trip; `-r`/`--refresh` and login still force it,
  and an empty playlist list triggers one revalidation.
- The status daemon now only attaches to the plugin's own mpv (verified by
  pid/start-time/executable/socket), refusing foreign sockets and logging the
  refusal. A manually launched mpv on the plugin socket is no longer mirrored.
- Playlist edit results are strictly classified; unrecognised responses are
  verified by re-reading the playlist instead of assuming every add succeeded.
- The panel no longer gets stuck with `busy`/`refreshing`/`searching` when
  `yt-music-ctl` is missing or fails to start: deadlines are armed at request
  time and clear the flow with a "backend unavailable" message.
- Stale backend responses no longer overwrite the current screen: library,
  playlist and search requests carry request tokens and are discarded if the
  user navigated away.
- Transport commands wait for the actual track change instead of a fixed delay,
  and queue edits use the row's stable key so a shifting queue cannot remove the
  wrong entry.
- The status daemon logs unexpected errors instead of silently retrying, and the
  installer refuses to delete a directory that resolves to its own source tree.
- UI: loading states for library/playlist loads, a distinct paused state in the
  bar, keyboard-navigable menus, and bounded thumbnail retries.
- The Up Next tab scrolls to the currently-playing row when it opens or the
  track changes, instead of always starting at the top of the queue. The list
  still holds the whole queue, but the top rows are no longer mistaken for the
  next track: the playing row (and the real next track below it) are in view.
- The Up Next track list now scrolls inside its own viewport: the now-playing
  card, the tab strip, and the UP NEXT header/action row stay fixed while the
  rows move, instead of scrolling the whole panel.
- That fixed-panel/scrollable-content layout now applies to every tab: the
  now-playing card and tab strip stay put, and each tab keeps its own header or
  action row pinned (Search field and scope pills, Last Played header, Playlists
  field, playlist/album/artist title bar, Library source buttons) while only the
  list scrolls in its own viewport.
- Right-click menus use a single highlight cursor again: hovering a row and the
  arrow keys drive the same selection, so a menu can no longer show two rows
  highlighted at once.
- Added a `cava`-driven audio visualizer below the now-playing card, with an
  on/off toggle, eight styles (bars, bars-mirror, voice-gradient, bars-meter, VU
  ladder, vertical VU, VU spectrum, VU dots),
  and mono/stereo plus linear/decibel display options; `install.sh` now notes the
  optional `cava` dependency and the README lists it.
- The linear/decibel visualizer option now works: cava 0.10.7 ignores the
  `scaling` config key, so the curve is applied to each parsed frame in the
  panel instead of being written to the cava config.
- Switching from one live station to another now refreshes Up Next instead of
  leaving the previous station's rows (both share the same queue key).
- A favourite add/remove that lands while a favourites fetch is already running
  is now replayed when that fetch exits, so the list can no longer stay stale.
- Station search no longer keeps showing the previous query's results or flashes
  "No stations found." while a new search is pending, surfaces backend errors in
  the status line, and pressing Enter no longer fires a duplicate lookup.
- Starting radio with the lyrics panel open now closes it, matching the hidden
  Lyrics button.
- Enter/Space on the Stations tab no longer toggles playback.
- The VU, vertical VU, and VU-spectrum meters no longer light their bottom
  segment at silence.

## 2.2.0 — 2026-09-30

- Renamed the plugin to **YouTube Music Bar** with the unique manifest id
  `io.github.maffur-hub.youtube-music-bar`, ahead of publishing on the Omarchy
  plugin marketplace. The previous bare id `yt-music` is permanently taken by
  the upstream plugin this project is forked from.
- Added marketplace install instructions, an uninstall note, a maintenance
  policy, and a bug-report issue template.
- Documented every feature, button, menu, keyboard shortcut, and CLI command in
  the README.
- Fixed the Log out button: the backend now implements and registers
  `yt-music-ctl logout`, which removes the locally stored authentication (it
  has no effect on the browser or the YouTube account). Previously the command
  was advertised in the help text but never implemented.
- Internal paths and the `yt-music-ctl` command are unchanged.

## 2.1.0 and earlier

See the git history for the phase- and stage-by-stage feature development
(A–F, G1–G6, H1–H10, I1–I10, J–O): library and playlist management,
multi-select, lyrics, offline metadata and audio precache, MPRIS integration,
and album save/remove.
