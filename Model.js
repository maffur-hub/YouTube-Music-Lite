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
  search: String.fromCharCode(0xf002),      // nf-fa-search
  playlist: String.fromCharCode(0xf00b),    // nf-fa-list
  stop: String.fromCharCode(0xf04d),        // nf-fa-stop
  shuffle: String.fromCharCode(0xf049d),    // nf-md-shuffle
  repeat: String.fromCharCode(0xf0456),     // nf-md-repeat
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

function fmtPosition(pos, dur) {
  return fmtDuration(pos) + " / " + fmtDuration(dur)
}

function barLabel(status) {
  if (!status || !status.playing) return ""
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
  var parts = [status.title || "Unknown"]
  if (status.artist) parts.push(status.artist)
  if (status.album) parts.push(status.album)
  if (status.paused) parts.push("paused")
  return parts.join(" — ")
}

function isActive(status) {
  return !!(status && (status.playing || status.paused))
}

function truncate(text, maxLen) {
  var t = String(text || "")
  if (t.length <= maxLen) return t
  return t.substring(0, maxLen - 1) + "…"
}

// ---- multi-select helpers (pure; used by Panel.qml and scripts/model_test.js)

// Stable identity for a search row: songs key on videoId, everything else on
// its browseId, prefixed with the row kind so kinds never collide.
function rowKey(row) {
  if (!row) return ""
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

// Backend `playlist-add-items` tokens, one per row, in list order.
function rowsToTokens(rows) {
  var out = []
  if (!rows || rows.length === 0) return out
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i]
    if (!row) continue
    if (row.kind === "song" && row.videoId) out.push("v:" + row.videoId)
    else if (row.kind === "album" && row.browseId) out.push("a:" + row.browseId)
    else if (row.kind === "artist" && row.browseId) out.push("r:" + row.browseId)
    else if (row.kind === "playlist" && row.browseId) out.push("p:" + row.browseId)
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

if (typeof module !== "undefined") {
  module.exports = {
    ICON: ICON,
    parseStatus: parseStatus,
    fmtDuration: fmtDuration,
    fmtPosition: fmtPosition,
    barLabel: barLabel,
    tooltipText: tooltipText,
    isActive: isActive,
    isPlaying: isPlaying,
    truncate: truncate,
    rowKey: rowKey,
    selectedCount: selectedCount,
    withRowSelected: withRowSelected,
    toggleSelected: toggleSelected,
    selectedRange: selectedRange,
    rowsToTokens: rowsToTokens,
    allSongs: allSongs,
    videoIds: videoIds,
    filterByTitle: filterByTitle,
    queueTokens: queueTokens
  }
}
