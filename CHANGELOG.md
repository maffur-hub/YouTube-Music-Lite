# Changelog

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
