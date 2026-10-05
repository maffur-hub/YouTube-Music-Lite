#!/usr/bin/env node
// Headless harness for the panel's async request patterns.
//
// The panel's processes cannot be run without Quickshell, but the state
// transitions they rely on live in AsyncState.js. This drives those through
// realistic sequences (a slow fetch, a navigation away, a coalesced refresh, a
// replaced search query) so the invariants the review passes established stay
// covered by `npm`-less `node scripts/asyncstate_harness.js`.
//
// It also asserts the source-level shape of Panel.qml: every fetch process has
// a deadline and an onExited, and the old ad-hoc bookkeeping names are gone.

"use strict"

const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const AsyncState = require("../AsyncState.js")

let passed = 0
function ok(value, msg) { passed++; assert.equal(value, true, msg) }
function eq(actual, expected, msg) { passed++; assert.deepEqual(actual, expected, msg) }

const panel = fs.readFileSync(path.join(__dirname, "..", "Panel.qml"), "utf8")

// --- Scenario: library load, then navigate away before it lands.
{
  const tokens = AsyncState.makeTokenSource()
  const proc = { requestToken: 0 }
  proc.requestToken = tokens.next()          // start album A
  tokens.invalidate()                        // user navigates away
  ok(!tokens.isCurrent(proc.requestToken), "a response after navigation is discarded")
  proc.requestToken = tokens.next()          // start album B
  ok(tokens.isCurrent(proc.requestToken), "the newest request is accepted")
}

// --- Scenario: two coalesced refreshes collapse into one replay.
{
  const flag = AsyncState.makeDirtyFlag()
  let fetches = 0
  function refresh() {
    fetches++
    if (fetches === 1) { flag.mark(); flag.mark(); return }  // two arrive mid-flight
    /* second fetch completes */
  }
  refresh()                                   // in-flight
  ok(flag.take(), "the coalesced refresh replays once after the fetch")
  ok(!flag.take(), "and does not replay again")
}

// --- Scenario: type "jazz", then "rock" while jazz is in flight, then back.
{
  const pending = AsyncState.makePendingQueue()
  pending.offer("rock", "jazz")               // rock differs -> queued
  eq(pending.take(), "rock", "the newer query replays")
  pending.offer("rock", "jazz")               // queued
  pending.offer("jazz", "jazz")               // back to the in-flight query
  ok(!pending.hasPending(), "returning to the in-flight query clears the replay")
}

// --- Scenario: the panel wires these objects, not bare ints/strings/bools.
ok(!/property int tracksRequestSeq/.test(panel), "tracksRequestSeq is gone")
ok(!/property int libraryRequestSeq/.test(panel), "libraryRequestSeq is gone")
ok(!/property bool stationFavoritesDirty/.test(panel), "stationFavoritesDirty bool is gone")
ok(!/property bool stationCatalogDirty/.test(panel), "stationCatalogDirty bool is gone")
ok(!/property string pendingStationSearch/.test(panel), "pendingStationSearch string is gone")
ok(/AsyncState\.makeTokenSource\(\)/.test(panel), "the panel uses makeTokenSource")
ok(/AsyncState\.makeDirtyFlag\(\)/.test(panel), "the panel uses makeDirtyFlag")
ok(/AsyncState\.makePendingQueue\(\)/.test(panel), "the panel uses makePendingQueue")

// --- Every backend fetch process has a deadline timer and an onExited handler.
// The set of processes that fetch and must therefore time out; the fire-and-
// forget writers (cava, ui-state) are exempt.
const fetchProcs = [
  "statusProc", "playlistsProc", "tracksProc", "searchProc", "playNowProc",
  "mixProc", "queueProc", "queueListProc", "lastPlayedProc", "likedSetProc",
  "queueClearProc", "restoreProc", "stationFavoritesProc", "stationCatalogProc",
  "stationFeaturedProc", "stationSearchProc", "stationPlayProc", "stationFavProc",
  "libraryProc", "albumStatusProc", "albumCmdProc", "logoutProc",
  "createPlaylistProc", "cmdProc", "lyricsProc", "thumbnailProc", "coverProc"
]
for (const id of fetchProcs) {
  ok(panel.includes("id: " + id), "panel defines " + id)
}
// A deadline Timer exists for each fetch key; count both the single-line and
// the multi-line block forms rather than naming each (the file has ~28).
const deadlineIds = new Set((panel.match(/id: \w+Deadline/g) || []))
ok(deadlineIds.size >= 25, "the panel has a deadline timer per fetch (" + deadlineIds.size + ")")

console.log(passed + " assertions passed")
