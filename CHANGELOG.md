# Changelog

## Unreleased

Review-driven fixes (see `docs/review-findings.md`, `docs/phase2-decisions.md`).

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
