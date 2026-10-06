// Complete in-panel help guide for YouTube Music Bar.
//
// Plain data only: Panel.qml renders each section in the scrollable overlay
// opened by the "?" button beside the tabs. Keep the wording beginner-facing —
// assume the reader has never seen this plugin before.

var SECTIONS = [
  {
    title: "What this is",
    lines: [
      "YouTube Music Bar is a YouTube Music player that lives in your",
      "Omarchy bar. It searches and browses your library, plays audio",
      "through mpv and yt-dlp, and shows what is playing on the bar.",
      "You do not need a paid subscription: the free tier plays, and",
      "account features (playlists, likes, your library) come from",
      "your own YouTube Music login."
    ]
  },
  {
    title: "Install (once)",
    lines: [
      "From the Omarchy plugin marketplace, add and enable the widget:",
      "    omarchy plugin add https://github.com/maffur-hub/YouTube-Music-Lite.git --enable",
      "Then run the bundled installer (it builds the Python backend):",
      "    ~/.config/omarchy/plugins/io.github.maffur-hub.youtube-music-bar/install.sh",
      "Finally check the prerequisites:",
      "    yt-music-ctl doctor",
      "Required: Omarchy with the Quickshell bar, python3, mpv, yt-dlp.",
      "Optional: mpv-mpris (media keys + desktop media widget), cava",
      "(the audio visualizer), libnotify (track-change notifications)."
    ]
  },
  {
    title: "First run and login",
    lines: [
      "Open the panel by clicking the music note on the bar.",
      "While logged out you see a 'Not logged in' card. Click",
      "'Login to YouTube Music': a terminal runs 'yt-music-ctl login',",
      "which reads cookies from Chromium first, then Firefox. If",
      "neither is found it lets you paste request headers by hand.",
      "Log in to YouTube Music in the browser window that opens. The",
      "panel polls for about 100 seconds, then reports if it could not",
      "detect the login.",
      "'Log out' (right of the Search field) only deletes the locally",
      "stored authentication. It does not touch your browser or your",
      "YouTube account.",
      "Privacy: cookies are stored only on this machine, in",
      "~/.config/yt-music/auth.json, with owner-only permissions."
    ]
  },
  {
    title: "The bar widget",
    lines: [
      "Music note    always visible; turns accent-coloured while playing.",
      "              While playing it also shows 'title - artist', cut",
      "              off at about 40 characters.",
      "Left-click    open or close the panel.",
      "Middle-click  toggle play/pause.",
      "Right-click   transport menu: Play/Pause, Previous, Next, Stop,",
      "              and Open player / Close player.",
      "Hover         tooltip with title, artist and album; adds",
      "              ' - paused' while paused, or 'not logged in'.",
      "There is no scroll gesture on the bar."
    ]
  },
  {
    title: "The panel at a glance",
    lines: [
      "Top     album art, a NOW PLAYING (or LAST PLAYED) card with the",
      "        title, artist and elapsed/total time.",
      "Then    the transport row, shown only while playing or paused.",
      "Then    the seek slider and the volume slider with a percentage.",
      "Then    the Lyrics button and the visualizer controls.",
      "Then    the tab strip, with the ? help button on the right.",
      "Bottom  a status line for every action; it fades after 4 seconds."
    ]
  },
  {
    title: "Transport controls",
    lines: [
      "Previous, Play/Pause, Next, Like (heart), Dislike, Shuffle, Stop,",
      "Repeat/Loop and Mute. A control is accent-coloured while active.",
      "Like toggles. Dislike is offered only on the now-playing card",
      "(right-click the card). Mute remembers and restores the volume.",
      "Drag the seek slider to scrub; release to seek. Drag the volume",
      "slider (0-100), or hover it and scroll, to change the volume."
    ]
  },
  {
    title: "Tabs",
    lines: [
      "Next       the play queue; labelled 'Next (N)' with tracks, and",
      "           always present.",
      "Search     search the YouTube Music catalog.",
      "History    this machine's local play history, with a Clear button.",
      "Playlists  your playlists (only while logged in).",
      "Library    Home, Recent, Liked, Songs, Albums, Artists (logged in).",
      "Stations   free internet radio; needs no YouTube login.",
      "Clicking the tab you are already on collapses it, or pops back",
      "one level from an open playlist, album or artist."
    ]
  },
  {
    title: "Search",
    lines: [
      "Type in 'Lookup tunes...' to search as you type (about a 450 ms",
      "delay), or press Enter to run it now. The x button clears the",
      "field and the results.",
      "The scope pills (Songs, Albums, Artists, Playlists) re-run the",
      "search. On the Songs scope, 'Play all' and 'Queue all' act on",
      "every result.",
      "Rows show 'artist - album'; songs also show their duration, a",
      "Play button and a Start-mix button. Albums, artists and",
      "playlists open in place under the Search tab; albums and",
      "artists have a Back button that returns to the results.",
      "'Select' turns on multi-select (see 'Selecting rows')."
    ]
  },
  {
    title: "History (Last Played)",
    lines: [
      "Newest first. Play a row, or right-click it for the normal song",
      "actions. The Clear button empties the local history only."
    ]
  },
  {
    title: "Playlists",
    lines: [
      "Create one with the 'New playlist name' field and Create (new",
      "playlists are private). Click a playlist to open its tracks; the",
      "arrow button queues and plays the whole playlist.",
      "The '...' options menu offers Rename playlist, Make public,",
      "Make private, Make unlisted and Delete playlist... (deleting",
      "asks for confirmation first).",
      "Track rows have Play and Start-mix buttons. Right-click a track",
      "for Play now, Play next, Add to queue, Start mix, Like, Add to",
      "playlist..., Remove from playlist and Move up / Move down where",
      "they apply."
    ]
  },
  {
    title: "Library",
    lines: [
      "Six sources: Home, Recent, Liked, Songs, Albums, Artists.",
      "Album and artist pages open in place with cover art and details;",
      "Back returns up to 8 levels of history.",
      "Album pages have a Save/Remove button for your YouTube Music",
      "library. Artist pages have Start radio, plus similar artists.",
      "'Play all' and 'Queue all' act on the page's tracks."
    ]
  },
  {
    title: "Stations (internet radio)",
    lines: [
      "Three sections: Featured (bundled picks), Favorites (yours) and",
      "Search. Search by Name or Genre; results update as you type and",
      "on Enter.",
      "Click a station to play it. Right-click a station to Play it, to",
      "add/remove it from Featured, or to add/remove it from Favorites.",
      "Radio is free and needs no login, so it works while logged out.",
      "Live streams have no lyrics and are shown differently from songs."
    ]
  },
  {
    title: "Up Next (the queue)",
    lines: [
      "The current track is highlighted and shows a play glyph; every",
      "row has a Jump-to-track button.",
      "Right-click a row for Move up, Move down, Remove from queue,",
      "plus the normal song actions.",
      "Clear removes upcoming tracks (or clears the saved queue when",
      "nothing is playing). Save stores the queue as a new playlist.",
      "While stopped, the last queue is shown from the saved session",
      "with a Resume button; opening the panel restores it paused."
    ]
  },
  {
    title: "Selecting rows",
    lines: [
      "'Select' turns on select mode in Search and Up Next.",
      "Click rows to select. Ctrl-click toggles one row; Shift-click",
      "selects a range.",
      "The action bar shows 'N selected', 'Add to playlist...' and,",
      "when every selected row is a song, 'Queue' (or 'Remove' in Up",
      "Next).",
      "'Add to playlist...' opens a searchable picker. Choose a",
      "playlist, or type a name that is not listed to get",
      "'Create \"name\"', which creates it and adds the tracks at once.",
      "Adding reports already-present tracks and anything YouTube",
      "refused to add."
    ]
  },
  {
    title: "Lyrics and the visualizer",
    lines: [
      "Lyrics (while a song plays): the Lyrics button opens the section",
      "and it reloads when the track changes. Its x button closes it.",
      "The visualizer needs cava. The equalizer button toggles it; the",
      "sliders button cycles styles (Bars, Mirror, Gradient, Meter, VU,",
      "VU Vertical, VU Spectrum, VU Dots); the Mono/Stereo button picks",
      "channels; and LIN/dB picks linear or decibel scaling. Choices",
      "are remembered."
    ]
  },
  {
    title: "Keyboard shortcuts",
    lines: [
      "j / Down      move the list cursor down",
      "k / Up        move the list cursor up",
      "h / Left      seek 5 seconds back while playing",
      "l / Right     seek 5 seconds forward while playing",
      "Enter / Space play or open the highlighted item; in a delete",
      "              confirmation, confirm",
      "x / X         remove the highlighted queue or playlist row",
      "s             stop",
      "m             mute toggle",
      "r             repeat / loop toggle",
      "f             shuffle",
      "/             jump to Search and focus the search box",
      "c             close the panel",
      "Esc           close the panel, or dismiss a confirmation",
      "Shortcuts pause while a text field or an open menu has focus.",
      "Tab does not navigate in this panel, and there is no",
      "scroll-to-seek or scroll-to-volume gesture."
    ]
  },
  {
    title: "Right-click menus",
    lines: [
      "Bar icon       transport menu (Play/Pause, Prev, Next, Stop).",
      "Song row       Play now, Play next, Add to queue, Start mix,",
      "               Like, Add to playlist...",
      "Now-playing    the same list plus Dislike.",
      "Playlist track the song list plus Remove and Move up / Move down.",
      "Album row      Album info, Save to library, Play all, Add all to",
      "               queue, Add all to playlist..., Open.",
      "Artist row     Artist info, Start radio, Play all, Add all to",
      "               queue, Add all to playlist..., Open.",
      "Playlist row   Play all, Add all to queue, Add all to",
      "               playlist..., Open.",
      "Queue row      Move up, Move down, Remove from queue, plus the",
      "               normal song actions.",
      "Station row    Play station, Add/Remove Featured, Add/Remove",
      "               Favorites.",
      "Login-only actions stay hidden while logged out."
    ]
  },
  {
    title: "Command line",
    lines: [
      "yt-music-ctl drives everything the panel does. Run it with no",
      "arguments for the built-in help. Most commands print JSON.",
      "Playback  play, play-next, queue-add, pause, resume, toggle,",
      "          next, prev, seek, seek-pct, volume, stop, loop, shuffle",
      "Engage    like, dislike, unlike",
      "Account   login, logout",
      "Library   search, playlists, create-playlist, playlist, liked,",
      "          library, album, album-status, album-save, album-remove,",
      "          artist, radio, home, history, last-played, restore",
      "Editing   playlist-add, playlist-add-items, playlist-edit,",
      "          playlist-delete, playlist-move, remove",
      "Queue     queue-list, queue-clear, queue-jump, queue-remove,",
      "          queue-remove-keys, queue-move, queue, enqueue,",
      "          enqueue-files",
      "Media     thumbnail, image, precache, lyrics, mix",
      "Service   status, daemon, watch, ensure-daemon, daemon-stop,",
      "          doctor"
    ]
  },
  {
    title: "Troubleshooting",
    lines: [
      "Run 'yt-music-ctl doctor' first: it checks python3, mpv, yt-dlp,",
      "cava and mpv-mpris, and prints an install hint for anything",
      "missing.",
      "No sound, or media keys do nothing: install mpv-mpris with",
      "'sudo pacman -S mpv-mpris' and restart the player.",
      "No visualizer: install cava with 'sudo pacman -S cava'.",
      "Not logged in: run 'yt-music-ctl login' and paste request",
      "headers if the browser cookies are not detected.",
      "Backend unavailable: rerun the plugin's install.sh.",
      "When reporting a bug, include 'yt-music-ctl doctor', your",
      "Omarchy version, and 'omarchy plugin list --json'."
    ]
  },
  {
    title: "About",
    lines: [
      "A best-effort personal project with no support SLA.",
      "MIT licensed. A fork of YouTube Music Lite by",
      "stevenwtlafrance-ship-it, substantially expanded by maffur-hub",
      "and distributed as YouTube Music Bar."
    ]
  }
]
