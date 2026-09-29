#!/usr/bin/env node
// Self-contained tests for the pure multi-select helpers in Model.js.
// Run from anywhere: `node scripts/model_test.js` (exits non-zero on failure).

"use strict"

const assert = require("node:assert/strict")
const Model = require("../Model.js")

let passed = 0
function eq(actual, expected, msg) {
  passed++
  assert.deepEqual(actual, expected, msg)
}
function ok(value, msg) {
  passed++
  assert.equal(value, true, msg)
}
function notOk(value, msg) {
  passed++
  assert.equal(value, false, msg)
}

const song = { kind: "song", videoId: "abcdefghijk", browseId: "", title: "Song" }
const album = { kind: "album", videoId: "", browseId: "ALBUMID001", title: "Album" }
const artist = { kind: "artist", videoId: "", browseId: "ARTISTID01", title: "Artist" }
const playlist = { kind: "playlist", videoId: "", browseId: "PLLISTID01", title: "Playlist" }
const junk = { kind: "video", videoId: "", browseId: "", title: "Junk" }

// --- rowKey
eq(Model.rowKey(null), "", "rowKey(null)")
eq(Model.rowKey(undefined), "", "rowKey(undefined)")
eq(Model.rowKey(song), "v:abcdefghijk", "rowKey song")
eq(Model.rowKey(album), "album:ALBUMID001", "rowKey album")
eq(Model.rowKey(artist), "artist:ARTISTID01", "rowKey artist")
eq(Model.rowKey(playlist), "playlist:PLLISTID01", "rowKey playlist")
eq(Model.rowKey({ key: "q:3", kind: "song", videoId: "", browseId: "" }), "q:3", "rowKey honours explicit key")
eq(Model.rowKey({ kind: "song", videoId: "abcdefghijk", browseId: "", key: "" }), "v:abcdefghijk", "empty key falls back to videoId")

// --- selectedCount
eq(Model.selectedCount(null), 0, "selectedCount(null)")
eq(Model.selectedCount(undefined), 0, "selectedCount(undefined)")
eq(Model.selectedCount("ab"), 0, "selectedCount(non-object)")
eq(Model.selectedCount(7), 0, "selectedCount(number)")
eq(Model.selectedCount({}), 0, "selectedCount(empty)")
eq(Model.selectedCount({ "v:one": song, "album:two": album }), 2, "selectedCount(2 keys)")

// --- toggleSelected: add, then remove, and never mutate the input
let sel = Model.toggleSelected({}, song)
eq(Object.keys(sel), ["v:abcdefghijk"], "toggleSelected adds the rowKey")
eq(sel["v:abcdefghijk"], song, "toggleSelected stores the row itself")
eq(Model.selectedCount(sel), 1, "toggleSelected count is 1")

const before = Object.assign({}, sel)
let sel2 = Model.toggleSelected(sel, album)
eq(Object.keys(sel), ["v:abcdefghijk"], "input unchanged after second toggle")
eq(sel2["v:abcdefghijk"], song, "toggleSelected preserves prior keys")
eq(Model.selectedCount(sel2), 2, "toggleSelected grows selection")

const removed = Model.toggleSelected(sel2, song)
eq(Object.keys(removed).sort(), ["album:ALBUMID001"], "toggleSelected removes on second toggle")
eq(Model.selectedCount(sel2), 2, "source untouched by removal")
eq(sel, before, "original object still identical")

// toggling an object that never had the key adds it
eq(Model.selectedCount(Model.toggleSelected(null, song)), 1, "toggleSelected(null, row) works")

// --- selectedRange: inclusive, order-agnostic, clamped, additive
const rows = [song, album, artist, playlist]
const seeded = { "playlist:PLLISTID01": playlist } // pre-existing selection outside the range
const rangeA = Model.selectedRange(seeded, rows, 0, 2)
eq(Object.keys(rangeA).sort(),
  ["album:ALBUMID001", "artist:ARTISTID01", "playlist:PLLISTID01", "v:abcdefghijk"],
  "range 0..2 inclusive and keeps prior selection")

const rangeB = Model.selectedRange(seeded, rows, 2, 0)
eq(Object.keys(rangeB).sort(), Object.keys(rangeA).sort(), "range is order-agnostic")

const rangeC = Model.selectedRange({}, rows, -5, 99)
eq(Object.keys(rangeC).sort(),
  ["album:ALBUMID001", "artist:ARTISTID01", "playlist:PLLISTID01", "v:abcdefghijk"],
  "range is clamped to the array bounds")

eq(Model.selectedRange({}, [], 0, 3), {}, "range over an empty array")
eq(Model.selectedRange(null, rows, 1, 1), { "album:ALBUMID001": album }, "range with null seed")
eq(Object.keys(seeded), ["playlist:PLLISTID01"], "seed untouched by selectedRange")

const withHole = [song, null, artist]
eq(Object.keys(Model.selectedRange({}, withHole, 0, 2)).sort(),
  ["artist:ARTISTID01", "v:abcdefghijk"],
  "falsy rows inside the range are skipped")

// --- rowsToTokens
eq(Model.rowsToTokens([song, album, artist, playlist]),
  ["v:abcdefghijk", "a:ALBUMID001", "r:ARTISTID01", "p:PLLISTID01"],
  "rowsToTokens maps every supported kind, in order")
eq(Model.rowsToTokens([junk, { kind: "song" }, { kind: "album" }, null]),
  [], "rowsToTokens skips invalid/unknown rows")
eq(Model.rowsToTokens([]), [], "rowsToTokens([])")
eq(Model.rowsToTokens(null), [], "rowsToTokens(null)")
eq(Model.rowsToTokens([song, { kind: "song", videoId: "abcdefghijk", browseId: "" }]),
  ["v:abcdefghijk"], "rowsToTokens dedupes rows with the same videoId")
eq(Model.rowsToTokens([song, album, { kind: "song", videoId: "abcdefghijk", browseId: "" }]),
  ["v:abcdefghijk", "a:ALBUMID001"], "dedupe keeps the first-seen order")
eq(Model.rowsToTokens([{ kind: "song", videoId: "", browseId: "" }]),
  [], "rowsToTokens skips a song row with an empty videoId")
eq(Model.rowsToTokens([album, artist, playlist]),
  ["a:ALBUMID001", "r:ARTISTID01", "p:PLLISTID01"],
  "rowsToTokens still maps album/artist/playlist to a:/r:/p:")

// --- rowAddable
ok(Model.rowAddable(song), "rowAddable true for a song with a videoId")
ok(Model.rowAddable(album), "rowAddable true for an album with a browseId")
notOk(Model.rowAddable({ kind: "song", videoId: "" }), "rowAddable false for a song with no videoId")
notOk(Model.rowAddable(junk), "rowAddable false for an unknown kind")

// --- allSongs
ok(Model.allSongs([song, song]), "allSongs true for songs only")
ok(Model.allSongs([song]), "allSongs true for a single song")
notOk(Model.allSongs([]), "allSongs false for empty array")
notOk(Model.allSongs([song, album]), "allSongs false when mixed with an album")
notOk(Model.allSongs([album, artist]), "allSongs false without songs")
notOk(Model.allSongs([junk]), "allSongs false for unknown kind")
notOk(Model.allSongs(null), "allSongs false for null")
notOk(Model.allSongs([{ kind: "song", videoId: "" }]), "allSongs false for a song with no videoId")

// --- videoIds
eq(Model.videoIds([song, album, { kind: "song", videoId: "zzzzzzzzzzz" }]),
  ["abcdefghijk", "zzzzzzzzzzz"], "videoIds keeps song ids only")
eq(Model.videoIds([album, artist, playlist, junk]), [], "videoIds drops non-songs")
eq(Model.videoIds([]), [], "videoIds([])")
eq(Model.videoIds(undefined), [], "videoIds(undefined)")

// --- filterByTitle
const plA = { id: "PL1", title: "Road Trip" }
const plB = { id: "PL2", title: "roadhouse blues" }
const plC = { id: "PL3", title: "Chill Nights" }
const plUntitled = { id: "PL4" }
const plAll = [plA, plB, plC, plUntitled]

eq(Model.filterByTitle(plAll, ""), plAll, "filterByTitle empty query returns the input")
eq(Model.filterByTitle(plAll, "   "), plAll, "filterByTitle blank query returns the input")
eq(Model.filterByTitle(plAll, "ROAD"), [plA, plB], "filterByTitle is case-insensitive")
eq(Model.filterByTitle(plAll, "chill"), [plC], "filterByTitle matches a substring")
eq(Model.filterByTitle(plAll, "nights"), [plC], "filterByTitle skips rows without a title")
eq(Model.filterByTitle(plAll, "nothing"), [], "filterByTitle with no match returns []")
eq(Model.filterByTitle([plB, plA], "ro"), [plB, plA], "filterByTitle preserves order")
eq(Model.filterByTitle(null, "ro"), [], "filterByTitle(null)")
eq(Model.filterByTitle(undefined, "ro"), [], "filterByTitle(undefined)")
eq(Model.filterByTitle("Road Trip", "ro"), [], "filterByTitle(non-array)")

// --- queueTokens
eq(Model.queueTokens([song, { kind: "song", videoId: "zzzzzzzzzzz" }]),
  ["v:abcdefghijk", "v:zzzzzzzzzzz"], "queueTokens prefixes v: and keeps order")
eq(Model.queueTokens([song, junk, album, { kind: "song", videoId: "short" }, null]),
  ["v:abcdefghijk"], "queueTokens skips invalid videoIds")
eq(Model.queueTokens([junk, album]), [], "queueTokens([]) with nothing valid")
eq(Model.queueTokens([]), [], "queueTokens([])")
eq(Model.queueTokens(null), [], "queueTokens(null)")
eq(Model.queueTokens(undefined), [], "queueTokens(undefined)")

// --- queueKeyAt
eq(Model.queueKeyAt([{ key: "v:abcdefghijk#0" }], 0), "v:abcdefghijk#0",
  "queueKeyAt returns a video row's key")
eq(Model.queueKeyAt([{ key: "q:3" }], 0), "q:3", "queueKeyAt returns a local row's key")
eq(Model.queueKeyAt([{ key: "v:abcdefghijk#1" }], 0), "v:abcdefghijk#1",
  "queueKeyAt keeps the occurrence suffix (no rowKey fallback)")
eq(Model.queueKeyAt([{ key: "v:abcdefghijk#0" }, { key: "q:3" }], 1), "q:3",
  "queueKeyAt resolves by index into the snapshot")
eq(Model.queueKeyAt([{ key: "v:abcdefghijk#0" }], 5), "", "queueKeyAt index past the end")
eq(Model.queueKeyAt([{ key: "v:abcdefghijk#0" }], -1), "", "queueKeyAt index -1")
eq(Model.queueKeyAt(null, 0), "", "queueKeyAt(null rows)")
eq(Model.queueKeyAt(undefined, 0), "", "queueKeyAt(undefined rows)")
eq(Model.queueKeyAt([{}], 0), "", "queueKeyAt missing key")
eq(Model.queueKeyAt([{ key: 7 }], 0), "", "queueKeyAt non-string key")
eq(Model.queueKeyAt([{ key: null }], 0), "", "queueKeyAt null key")
eq(Model.queueKeyAt([null], 0), "", "queueKeyAt null row")
eq(Model.queueKeyAt([], 0), "", "queueKeyAt empty rows")

// --- idsToNames
const nameLabels = { aaa: "A", bbb: "B", ccc: "C" }
eq(Model.idsToNames(["aaa", "bbb"], nameLabels, 2), "A, B", "idsToNames names every known id")
eq(Model.idsToNames(["aaa", "bbb", "ccc"], nameLabels, 2), "A, B …",
  "idsToNames appends an ellipsis when ids exceed max")
eq(Model.idsToNames(["aaa", "zzz"], nameLabels, 2), "", "idsToNames returns \"\" when any id is unknown")
eq(Model.idsToNames([], nameLabels, 2), "", "idsToNames([])")
eq(Model.idsToNames(undefined, nameLabels, 2), "", "idsToNames(undefined)")

console.log(passed + " assertions passed")
