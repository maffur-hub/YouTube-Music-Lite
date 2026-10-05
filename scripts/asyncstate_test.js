#!/usr/bin/env node
// Tests for AsyncState.js — the token/dirty/pending bookkeeping shared by the
// panel's backend processes. Run: `node scripts/asyncstate_test.js`.

"use strict"

const assert = require("node:assert/strict")
const AsyncState = require("../AsyncState.js")

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

// --- makeTokenSource
const tokens = AsyncState.makeTokenSource()
const t1 = tokens.next()
ok(tokens.isCurrent(t1), "a freshly issued token is current")
const t2 = tokens.next()
ok(tokens.isCurrent(t2), "the newest token is current")
notOk(tokens.isCurrent(t1), "an older token is no longer current")
tokens.invalidate()
notOk(tokens.isCurrent(t2), "invalidate drops the outstanding token")
const t3 = tokens.next()
ok(tokens.isCurrent(t3), "a token issued after invalidate is current")
ok(t3 > t2, "tokens are monotonic")
eq(tokens.current(), t3, "current() reports the latest token")

// --- makeDirtyFlag
const dirty = AsyncState.makeDirtyFlag()
notOk(dirty.isDirty(), "a fresh flag is clean")
eq(dirty.take(), false, "take() on a clean flag is false and stays clean")
dirty.mark()
ok(dirty.isDirty(), "mark() sets the flag")
eq(dirty.take(), true, "take() returns true once")
notOk(dirty.isDirty(), "take() clears the flag")
eq(dirty.take(), false, "a second take() after consumption is false")
dirty.mark(); dirty.mark()
eq(dirty.take(), true, "repeated marks still consume once")
eq(dirty.take(), false, "and only once")
dirty.mark(); dirty.clear()
notOk(dirty.isDirty(), "clear() drops a marked flag")

// --- makePendingQueue
const pending = AsyncState.makePendingQueue()
notOk(pending.hasPending(), "a fresh queue has nothing pending")
eq(pending.take(), "", "take() on an empty queue is \"\"")

// offer a genuinely newer query: it is stored.
pending.offer("rock", "jazz")
ok(pending.hasPending(), "a different query is queued")
eq(pending.take(), "rock", "take() returns the newest query")
notOk(pending.hasPending(), "take() consumed it")

// offer the same query as the in-flight one: nothing to replay.
pending.offer("jazz", "jazz")
notOk(pending.hasPending(), "an identical in-flight query is not queued")

// offering the in-flight query also clears an older pending value.
pending.offer("rock", "jazz")
pending.offer("jazz", "jazz")
notOk(pending.hasPending(), "returning to the in-flight query clears the pending one")

// clear() drops a pending query.
pending.offer("rock", "jazz")
pending.clear()
notOk(pending.hasPending(), "clear() drops a pending query")

// numeric/undefined coercion goes through String().
pending.offer(42, "42")
notOk(pending.hasPending(), "42 and \"42\" compare equal")
pending.offer(undefined, "")
notOk(pending.hasPending(), "undefined and \"\" compare equal")

console.log(passed + " assertions passed")
