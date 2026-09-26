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
  shuffle: String.fromCharCode(0xf074),     // nf-fa-random
  repeat: String.fromCharCode(0xf036),      // nf-fa-repeat
  login: String.fromCharCode(0xf2f6),       // nf-fa-right_to_bracket
  logout: String.fromCharCode(0xf2f5),      // nf-fa-right_from_bracket
  close: String.fromCharCode(0xf00d),       // nf-fa-times
  music: String.fromCharCode(0xf3d5),       // nf-fa-compact-disc
  plus: String.fromCharCode(0xf067),        // nf-fa-plus
  check: String.fromCharCode(0xf00c),       // nf-fa-check
  arrowLeft: String.fromCharCode(0xf060),   // nf-fa-arrow_left
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
    truncate: truncate
  }
}
