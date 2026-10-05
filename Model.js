// yt-music — formatting helpers for YouTube Music status cache.

const ICON = {
  note: String.fromCharCode(0xf001),        // nf-fa-music
  volume: String.fromCharCode(0xf028),      // nf-fa-volume_up
  play: String.fromCharCode(0xf04b),        // nf-fa-play
  pause: String.fromCharCode(0xf04c),       // nf-fa-pause
  next: String.fromCharCode(0xf051),        // nf-fa-forward
  prev: String.fromCharCode(0xf048),        // nf-fa-backward
  like: String.fromCharCode(0xf004),        // nf-fa-heart
  dislike: String.fromCharCode(0xf165),     // nf-fa-thumbs_down
  star: String.fromCharCode(0xf005),        // nf-fa-star
  starOutline: String.fromCharCode(0xf006), // nf-fa-star_o
  search: String.fromCharCode(0xf002),      // nf-fa-search
  playlist: String.fromCharCode(0xf00b),    // nf-fa-list
  stop: String.fromCharCode(0xf04d),        // nf-fa-stop
  shuffle: String.fromCodePoint(0xf049d),   // nf-md-shuffle
  repeat: String.fromCodePoint(0xf0456),    // nf-md-repeat
  login: String.fromCharCode(0xf2f6),       // nf-fa-right_to_bracket
  logout: String.fromCharCode(0xf2f5),      // nf-fa-right_from_bracket
  close: String.fromCharCode(0xf00d),       // nf-fa-times
  music: String.fromCharCode(0xf3d5),       // nf-fa-compact-disc
  plus: String.fromCharCode(0xf067),        // nf-fa-plus
  check: String.fromCharCode(0xf00c),       // nf-fa-check
  arrowLeft: String.fromCharCode(0xf060),   // nf-fa-arrow_left
  more: String.fromCharCode(0xf142),        // nf-fa-ellipsis_v
  pencil: String.fromCharCode(0xf040),      // nf-fa-pencil
  trash: String.fromCharCode(0xf1f8),       // nf-fa-trash
  arrowUp: String.fromCharCode(0xf062),     // nf-fa-arrow_up
  arrowDown: String.fromCharCode(0xf063),   // nf-fa-arrow_down
  globe: String.fromCharCode(0xf0ac),       // nf-fa-globe
  lock: String.fromCharCode(0xf023),        // nf-fa-lock
  save: String.fromCharCode(0xf0c7),        // nf-fa-save
  equalizer: String.fromCharCode(0xf080),   // nf-fa-bar_chart
  sliders: String.fromCharCode(0xf1de),     // nf-fa-sliders
}

function parseStatus(raw) {
  try {
    var data = JSON.parse(String(raw || ""))
    if (!data || data.ok !== true) return null
    return data
  } catch (e) {
    return null
  }
}

function fmtDuration(secs) {
  var s = parseInt(String(secs || "0"), 10)
  if (isNaN(s) || s < 0) return "0:00"
  var m = Math.floor(s / 60)
  var r = s % 60
  return m + ":" + (r < 10 ? "0" : "") + r
}

function barLabel(status) {
  if (!status || !status.playing) return ""
  if (status.live === true) {
    // A live radio station has no track to name: show the station cleanly
    // instead of appending the ICY now-playing string as if it were an artist.
    var station = status.stationName || status.title || ""
    return truncate(station, 40)
  }
  var title = status.title || ""
  var artist = status.artist || ""
  var text = artist ? (title + " · " + artist) : title
  return truncate(text, 40)
}

function isPlaying(status) {
  return !!(status && status.playing)
}

function tooltipText(status) {
  if (!status) return "YouTube Music — not logged in"
  if (!status.playing && !status.paused) return "YouTube Music — idle"
  if (status.live === true) {
    var live = ["LIVE", status.stationName || status.title || "Radio"]
    if (status.nowPlaying) live.push(status.nowPlaying)
    if (status.paused) live.push("paused")
    return live.join(" — ")
  }
  var parts = [status.title || "Unknown"]
  if (status.artist) parts.push(status.artist)
  if (status.album) parts.push(status.album)
  if (status.paused) parts.push("paused")
  return parts.join(" — ")
}

function isActive(status) {
  return !!(status && (status.playing || status.paused))
}

// One cava raw-ASCII frame -> an array of `count` bar heights in 0..100.
// Fields missing from the line (short frame) or unparseable become 0; values
// outside the range are clamped. Never returns undefined entries.
function parseCavaFrame(line, count) {
  var n = Math.max(0, Math.floor(Number(count) || 0))
  var fields = String(line || "").trim().split(";")
  var out = []
  for (var i = 0; i < n; i++) {
    var v = Number(fields[i])
    if (isNaN(v)) v = 0
    out.push(Math.max(0, Math.min(100, v)))
  }
  return out
}

// Highest value in a cava frame (0 when empty), for VU/peak meters.
function cavaPeak(bars) {
  if (!Array.isArray(bars) || bars.length === 0) return 0
  var m = 0
  for (var i = 0; i < bars.length; i++) {
    var v = Number(bars[i])
    if (!isNaN(v) && v > m) m = v
  }
  return Math.max(0, Math.min(100, m))
}

// VU level for a cava frame: a blend of the 75th percentile and the peak, so
// one loud band cannot peg the meter (autosens drives the max to ~100).
function cavaLevel(bars) {
  if (!Array.isArray(bars) || bars.length === 0) return 0
  var sorted = []
  for (var i = 0; i < bars.length; i++) {
    var v = Number(bars[i])
    if (!isNaN(v)) sorted.push(Math.max(0, Math.min(100, v)))
  }
  if (sorted.length === 0) return 0
  sorted.sort(function(a, b) { return a - b })
  var p75 = sorted[Math.min(sorted.length - 1, Math.floor(0.75 * sorted.length))]
  var peak = sorted[sorted.length - 1]
  return Math.max(0, Math.min(100, 0.75 * p75 + 0.25 * peak))
}

// Next per-band peak-hold values: each band jumps to its level and decays.
// `previous` is the prior array (may be missing); `decay` is per frame.
function cavaBandPeaks(bars, previous, decay) {
  var out = []
  var n = Array.isArray(bars) ? bars.length : 0
  var d = Number(decay)
  if (isNaN(d)) d = 4
  for (var i = 0; i < n; i++) {
    var v = Number(bars[i])
    if (isNaN(v)) v = 0
    var p = Array.isArray(previous) ? Number(previous[i]) : 0
    if (isNaN(p)) p = 0
    out.push(Math.max(Math.max(0, Math.min(100, v)), Math.max(0, p - d)))
  }
  return out
}

// Scale every band by `scale` (clamped low at 0; values may exceed 1 to
// boost), clamping each result to 0..100. Used both to give the spectrum
// headroom (tallest column maps to the damped VU level) and to apply display
// gain (volume * visualizerGain).
function cavaScaleBars(bars, scale) {
  var out = []
  var s = Number(scale)
  // Reject a non-finite scale (NaN/Infinity): Math.min(100, 0 * Infinity) is
  // NaN, so an infinite factor must collapse to the identity, not poison the
  // frame with NaN heights.
  if (!isFinite(s)) s = 1
  s = Math.max(0, s)
  if (Array.isArray(bars)) {
    for (var i = 0; i < bars.length; i++) {
      var v = Number(bars[i])
      if (isNaN(v)) v = 0
      var scaled = v * s
      out.push(isFinite(scaled) ? Math.max(0, Math.min(100, scaled)) : 100)
    }
  }
  return out
}

// The visualizer's LIN/dB option, applied to the parsed 0..100 frame. cava
// 0.10.7 (the released version) does not understand the `scaling` config key
// (only unreleased master does), so the choice lives in the frontend instead of
// the generated config. Everything but "decibel" is an identity copy; the
// decibel curve lifts quiet bands while keeping the endpoints (0 -> 0,
// 100 -> 100) fixed. Non-arrays yield []; NaN entries are treated as 0.
function cavaApplyScaling(bars, scaling) {
  if (!Array.isArray(bars)) return []
  if (scaling !== "decibel") return bars.slice()
  var out = []
  for (var i = 0; i < bars.length; i++) {
    var v = Number(bars[i])
    if (isNaN(v)) v = 0
    v = Math.max(0, Math.min(100, v))
    var scaled = 100 * Math.log(1 + 9 * v / 100) / Math.LN10
    out.push(Math.max(0, Math.min(100, scaled)))
  }
  return out
}

// Display gain from the player volume (0..150 maps to 0..1.5); NaN -> 1.
function cavaVolumeScale(volume) {
  var v = Number(volume)
  if (isNaN(v)) v = 100
  return Math.max(0, Math.min(1.5, v / 100))
}

// Classic LED zone for a 0..100 level: "green" < 60, "amber" < 85, else "red".
function cavaZone(level) {
  var v = Number(level)
  if (isNaN(v)) v = 0
  if (v < 60) return "green"
  if (v < 85) return "amber"
  return "red"
}

// Complete cava config written to the runtime path. Display-only: changing
// channels never affects audio playback. The linear/decibel choice is applied
// in the frontend (see cavaApplyScaling) because cava 0.10.7 ignores `scaling`.
function cavaConfig(channels) {
  var ch = channels === "mono" ? "mono" : "stereo"
  return "[general]\n"
    + "bars = 24\n"
    + "framerate = 30\n"
    + "autosens = 1\n"
    + "sensitivity = 100\n"
    + "live-config = 1\n"
    + "\n[input]\n"
    + "method = pulse\n"
    + "source = auto\n"
    + "\n[output]\n"
    + "method = raw\n"
    + "raw_target = /dev/stdout\n"
    + "data_format = ascii\n"
    + "ascii_max_range = 100\n"
    + "bar_delimiter = 59\n"
    + "frame_delimiter = 10\n"
    + "channels = " + ch + "\n"
    + "mono_option = average\n"
    + "\n[smoothing]\n"
    + "noise_reduction = 65\n"
    + "monstercat = 1.2\n"
}

function truncate(text, maxLen) {
  var t = String(text || "")
  if (t.length <= maxLen) return t
  return t.substring(0, maxLen - 1) + "…"
}

// ---- backend-row normalization (pure; used by Panel.qml)
//
// These shape backend JSON into the row objects the panel renders. They lived
// in Panel.qml as methods, tangled with QML state; here they are pure and
// unit-tested, so a backend shape change is caught by model_test.js instead of
// only by hand.

function boundedString(value, limit) {
  return String(value === undefined || value === null ? "" : value).slice(0, limit)
}

function isVideoId(value) {
  return /^[A-Za-z0-9_-]{11}$/.test(String(value || ""))
}

function normalizeSong(song) {
  if (!song || !isVideoId(song.videoId)) return null
  return {
    videoId: String(song.videoId),
    title: boundedString(song.title, 256),
    artist: boundedString(song.artist, 256),
    album: boundedString(song.album, 256),
    duration: Math.max(0, Math.min(86400, Number(song.duration) || 0))
  }
}

function normalizeSongs(items, limit) {
  var result = []
  var count = Math.min(Array.isArray(items) ? items.length : 0, limit)
  for (var i = 0; i < count; i++) {
    var song = normalizeSong(items[i])
    if (song) result.push(song)
  }
  return result
}

function normalizeMixedRows(items, limit) {
  var out = []
  var count = Math.min(Array.isArray(items) ? items.length : 0, limit)
  for (var i = 0; i < count; i++) {
    var item = items[i] || {}
    var kind = String(item.kind || "")
    if (kind === "song") {
      var vid = String(item.videoId || "")
      if (!isVideoId(vid)) continue
      out.push({ kind: "song", videoId: vid, browseId: "",
                 title: boundedString(item.title, 256),
                 artist: boundedString(item.artist, 256),
                 album: boundedString(item.album, 256),
                 duration: Math.max(0, Math.min(86400, Number(item.duration) || 0)) })
    } else if (kind === "album" || kind === "artist" || kind === "playlist") {
      var bid = boundedString(item.browseId, 256)
      if (!bid) continue
      out.push({ kind: kind, videoId: "", browseId: bid,
                 title: boundedString(item.title, 256),
                 artist: boundedString(item.artist, 256), duration: 0 })
    }
  }
  return out
}

function normalizeStations(items) {
  var out = []
  var count = Math.min(Array.isArray(items) ? items.length : 0, 100)
  for (var i = 0; i < count; i++) {
    var item = items[i] || {}
    var tags = []
    if (Array.isArray(item.tags)) {
      for (var t = 0; t < item.tags.length && tags.length < 32; t++) {
        var tag = boundedString(item.tags[t], 64).trim()
        if (tag !== "") tags.push(tag)
      }
    }
    out.push({
      kind: "station",
      id: boundedString(item.id, 256),
      name: boundedString(item.name, 256),
      url: boundedString(item.url, 1024),
      favicon: boundedString(item.favicon, 1024),
      homepage: boundedString(item.homepage, 1024),
      tags: tags,
      country: boundedString(item.country, 16),
      codec: boundedString(item.codec, 32),
      bitrate: Math.max(0, Number(item.bitrate) || 0),
      source: boundedString(item.source, 32)
    })
  }
  return out
}

function normalizePlaylists(items) {
  var result = []
  var count = Math.min(Array.isArray(items) ? items.length : 0, 100)
  for (var i = 0; i < count; i++) {
    var playlist = items[i]
    if (!playlist || !playlist.id) continue
    result.push({
      id: boundedString(playlist.id, 256),
      title: boundedString(playlist.title, 256),
      description: boundedString(playlist.description, 256)
    })
  }
  return result
}

// "artist · album", or whichever of the two exists.
function songSubtitle(row) {
  if (!row) return ""
  var artist = String(row.artist || "")
  var album = String(row.album || "")
  if (artist && album) return artist + " · " + album
  return artist || album
}

// "country · tags · codec bitratek" for a station row, skipping empty parts.
function stationSubtitle(row) {
  if (!row) return ""
  var parts = []
  var country = boundedString(row.country, 16).trim()
  if (country !== "") parts.push(country)
  var tags = []
  if (Array.isArray(row.tags)) {
    for (var i = 0; i < row.tags.length && tags.length < 4; i++) {
      var tag = String(row.tags[i] || "").trim()
      if (tag !== "") tags.push(tag)
    }
  }
  if (tags.length > 0) parts.push(tags.join(", "))
  var codec = boundedString(row.codec, 32).trim()
  var bitrate = Math.max(0, Number(row.bitrate) || 0)
  if (codec !== "" && bitrate > 0) parts.push(codec + " " + Math.round(bitrate) + "k")
  else if (codec !== "") parts.push(codec)
  else if (bitrate > 0) parts.push(Math.round(bitrate) + "k")
  return parts.join(" · ")
}

// Number of song rows in a mixed list (albums/artists/playlists excluded).
function songCount(rows) {
  var n = 0
  if (!Array.isArray(rows)) return 0
  for (var i = 0; i < rows.length; i++)
    if (rows[i] && rows[i].kind === "song") n++
  return n
}

// Rows after the current one, or 0 when the queue is empty/unknown.
function queueUpcomingCount(tracks, position) {
  var list = Array.isArray(tracks) ? tracks : []
  var remaining = list.length - 1
  var pos = (typeof position === "number") ? position : 0
  if (pos >= 0) remaining -= pos
  return Math.max(0, remaining)
}

// Index of the currently-playing/queued row, or -1. Prefers mpv's reported
// position, falling back to the row flagged `current` from the backend so the
// saved-queue (nothing playing) view works too.
function queueCurrentIndex(tracks, position) {
  var list = Array.isArray(tracks) ? tracks : []
  for (var i = 0; i < list.length; i++) {
    var row = list[i]
    if ((row && row.current) || i === position) return i
  }
  return (position >= 0 && position < list.length) ? position : -1
}

// ---- multi-select helpers (pure; used by Panel.qml and scripts/model_test.js)

// Stable identity for a search row: songs key on videoId, everything else on
// its browseId, prefixed with the row kind so kinds never collide.
function rowKey(row) {
  if (!row) return ""
  if (row.key) return String(row.key)
  if (row.kind === "station") return "st:" + String(row.id || "")
  if (row.kind === "song") return "v:" + row.videoId
  return String(row.kind) + ":" + String(row.browseId)
}

function selectedCount(selected) {
  if (!selected || typeof selected !== "object") return 0
  return Object.keys(selected).length
}

// Returns a NEW object; never mutates `selected`.
function withRowSelected(selected, row, on) {
  var out = {}
  if (selected && typeof selected === "object") {
    var keys = Object.keys(selected)
    for (var i = 0; i < keys.length; i++) out[keys[i]] = selected[keys[i]]
  }
  var key = rowKey(row)
  if (on) out[key] = row
  else delete out[key]
  return out
}

function toggleSelected(selected, row) {
  return withRowSelected(selected, row, !(selected && selected[rowKey(row)]))
}

// Adds every row in the inclusive [a, b] index span (order-agnostic, clamped
// to the array); existing selections are always preserved.
function selectedRange(selected, rows, a, b) {
  var out = {}
  if (selected && typeof selected === "object") {
    var keys = Object.keys(selected)
    for (var i = 0; i < keys.length; i++) out[keys[i]] = selected[keys[i]]
  }
  if (!rows || rows.length === 0) return out
  var lo = Math.max(0, Math.min(a, b))
  var hi = Math.min(rows.length - 1, Math.max(a, b))
  for (var j = lo; j <= hi; j++) {
    var row = rows[j]
    if (!row) continue
    out[rowKey(row)] = row
  }
  return out
}

// True when a row contributes something to a `playlist-add-items` request:
// songs need a videoId, albums/artists/playlists a browseId.
function rowAddable(row) {
  if (!row) return false
  if (row.kind === "song") return !!row.videoId
  if (row.kind === "album" || row.kind === "artist" || row.kind === "playlist")
    return !!row.browseId
  return false
}

// Backend `playlist-add-items` tokens, one per addable row, in list order.
function rowsToTokens(rows) {
  var out = []
  if (!rows || rows.length === 0) return out
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i]
    if (!rowAddable(row)) continue
    var token
    if (row.kind === "song") token = "v:" + row.videoId
    else if (row.kind === "album") token = "a:" + row.browseId
    else if (row.kind === "artist") token = "r:" + row.browseId
    else token = "p:" + row.browseId
    // Selecting the same track twice must not inflate the count: the backend
    // dedupes it server-side, so drop repeated tokens here to keep the staged
    // count equal to the number of tracks that will really be added.
    if (out.indexOf(token) === -1) out.push(token)
  }
  return out
}

// True only for a non-empty list made up exclusively of playable songs.
function allSongs(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return false
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i]
    if (!row || row.kind !== "song" || !row.videoId) return false
  }
  return true
}

function videoIds(rows) {
  var out = []
  if (!Array.isArray(rows)) return out
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i]
    if (row && row.kind === "song" && row.videoId) out.push(String(row.videoId))
  }
  return out
}

function likedSet(items) {
  // Build a {videoId: true} lookup from a `liked` payload's items, so the panel
  // can colour the hero heart without re-scanning the list on every status tick.
  var out = {}
  if (!items) return out
  for (var i = 0; i < items.length; i++) {
    var item = items[i]
    if (!item) continue
    var vid = item.videoId
    if (typeof vid === "string" && vid !== "") out[vid] = true
  }
  return out
}

// ---- playlist helpers (pure; used by Panel.qml and scripts/model_test.js)

// Case-insensitive substring match on each item's `title`. An empty or
// whitespace-only query returns the input array untouched; items without a
// title are skipped and the original order is preserved. Drives the
// searchable playlist picker's filter row as well as plain list filtering.
function filterByTitle(items, query) {
  if (!Array.isArray(items)) return []
  var q = String(query === undefined || query === null ? "" : query).trim().toLowerCase()
  if (q === "") return items
  var out = []
  for (var i = 0; i < items.length; i++) {
    var item = items[i]
    if (!item || !item.title) continue
    if (String(item.title).toLowerCase().indexOf(q) !== -1) out.push(item)
  }
  return out
}

// Backend `playlist-add-items` video tokens for a queue of song rows, in
// queue order; rows without a well-formed 11-char videoId are skipped.
function queueTokens(tracks) {
  var out = []
  if (!Array.isArray(tracks)) return out
  for (var i = 0; i < tracks.length; i++) {
    var track = tracks[i]
    if (!track || !/^[A-Za-z0-9_-]{11}$/.test(String(track.videoId || ""))) continue
    out.push("v:" + String(track.videoId))
  }
  return out
}

function queueKeyAt(rows, index) {
  // The backend's queue-list gives every row a stable key: `v:<videoId>#<occurrence>`
  // for YouTube entries and `q:<index>` for local ones. Resolve through it so a
  // queue shift between render and command cannot remove the wrong track.
  if (!rows || index < 0 || index >= rows.length) return ""
  var row = rows[index]
  if (!row || typeof row.key !== "string") return ""
  return row.key
}

// Name up to `max` ids when EVERY id is known; otherwise return "" so the
// caller can fall back to a plain count. Adds a trailing " …" when the ids
// were truncated.
function idsToNames(ids, labels, max) {
  if (!ids || !ids.length) return ""
  var out = []
  for (var i = 0; i < ids.length; i++) {
    var name = labels ? labels[String(ids[i])] : ""
    if (!name) return ""
    if (i < max) out.push(name)
  }
  return ids.length > max ? out.join(", ") + " …" : out.join(", ")
}

if (typeof module !== "undefined") {
  module.exports = {
    ICON: ICON,
    parseStatus: parseStatus,
    fmtDuration: fmtDuration,
    barLabel: barLabel,
    tooltipText: tooltipText,
    isActive: isActive,
    isPlaying: isPlaying,
    parseCavaFrame: parseCavaFrame,
    cavaPeak: cavaPeak,
    cavaLevel: cavaLevel,
    cavaBandPeaks: cavaBandPeaks,
    cavaScaleBars: cavaScaleBars,
    cavaApplyScaling: cavaApplyScaling,
    cavaVolumeScale: cavaVolumeScale,
    cavaZone: cavaZone,
    cavaConfig: cavaConfig,
    truncate: truncate,
    boundedString: boundedString,
    isVideoId: isVideoId,
    normalizeSong: normalizeSong,
    normalizeSongs: normalizeSongs,
    normalizeMixedRows: normalizeMixedRows,
    normalizeStations: normalizeStations,
    normalizePlaylists: normalizePlaylists,
    songSubtitle: songSubtitle,
    stationSubtitle: stationSubtitle,
    songCount: songCount,
    queueUpcomingCount: queueUpcomingCount,
    queueCurrentIndex: queueCurrentIndex,
    rowKey: rowKey,
    selectedCount: selectedCount,
    withRowSelected: withRowSelected,
    toggleSelected: toggleSelected,
    selectedRange: selectedRange,
    rowAddable: rowAddable,
    rowsToTokens: rowsToTokens,
    allSongs: allSongs,
    videoIds: videoIds,
    likedSet: likedSet,
    filterByTitle: filterByTitle,
    queueTokens: queueTokens,
    queueKeyAt: queueKeyAt,
    idsToNames: idsToNames
  }
}
