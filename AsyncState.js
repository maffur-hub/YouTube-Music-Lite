// Async request bookkeeping shared by the panel's backend processes.
//
// Every fetch in Panel.qml follows one of three shapes, and each grew its own
// ad-hoc implementation (a request-token int, a `*Dirty` flag, or a
// `pendingSearch` string). These small objects give all of them one tested
// implementation so the patterns cannot drift:
//
//   makeTokenSource()  — monotonic tokens; a late response whose token no
//                        longer matches is discarded (library/tracks).
//   makeDirtyFlag()    — coalesced refresh: a request that arrives while a
//                        fetch is in flight is replayed once on exit.
//   makePendingQueue() — replay only the newest request, and treat an
//                        identical in-flight request as already satisfied.
//
// The panel keeps the QML objects (Process/Timer); these hold the pure state
// transitions so they can be unit-tested in Node.

function makeTokenSource() {
  var seq = 0
  return {
    // Start a new request: bump the token and hand it to the caller to stash
    // on the process.
    next: function() {
      seq++
      return seq
    },
    // Invalidate every outstanding request without starting a new one (e.g.
    // navigating away).
    invalidate: function() {
      seq++
    },
    // True when a response carrying `token` still owns the current request.
    isCurrent: function(token) {
      return token === seq
    },
    // The current token, for callers that need to compare directly.
    current: function() {
      return seq
    }
  }
}

function makeDirtyFlag() {
  var dirty = false
  return {
    // A refresh was requested while a fetch is in flight.
    mark: function() { dirty = true },
    // Whether a replay is owed, without consuming it.
    isDirty: function() { return dirty },
    // Consume the replay: true once, then false until marked again.
    take: function() {
      var was = dirty
      dirty = false
      return was
    },
    clear: function() { dirty = false }
  }
}

function makePendingQueue() {
  function norm(value) {
    // A missing query means "nothing": coerce null/undefined to "" rather than
    // the literal strings "null"/"undefined".
    return value === undefined || value === null ? "" : String(value)
  }
  var pending = ""
  return {
    // Queue `query` for after the in-flight request, unless it is identical to
    // what is already running (then there is nothing to replay and any older
    // pending value is dropped).
    offer: function(query, inflight) {
      pending = (norm(query) !== norm(inflight)) ? norm(query) : ""
    },
    // Drop any pending query (empty input, navigation away).
    clear: function() { pending = "" },
    hasPending: function() { return pending !== "" },
    // Consume and return the pending query ("" when none).
    take: function() {
      var value = pending
      pending = ""
      return value
    }
  }
}

if (typeof module !== "undefined") {
  module.exports = {
    makeTokenSource: makeTokenSource,
    makeDirtyFlag: makeDirtyFlag,
    makePendingQueue: makePendingQueue
  }
}
