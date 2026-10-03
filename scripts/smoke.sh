#!/usr/bin/env bash
#
# Smoke test for the yt-music Omarchy plugin backend (yt-music-ctl).
#
# Usage: scripts/smoke.sh [--full] [--mutating]
#
#   scripts/smoke.sh          Non-destructive run. Exercises the read-only
#                             commands, the network navigation lookups, the
#                             metadata response cache and the
#                             usage/argument-error checks. Nothing on the
#                             YouTube Music account is mutated and no audio is
#                             played.
#   scripts/smoke.sh --full   Also exercises the playback-affecting commands:
#                             starts playback of a known videoId, pokes the
#                             transport controls and the queue, then cleans up
#                             with `stop` + `daemon-stop`. Also plays the
#                             artist's radio, precaches one known track's
#                             audio (a real yt-dlp download) and checks the
#                             precache answers without a player. While a track
#                             is playing it checks the MPRIS player behind the
#                             media keys / Omarchy media widget (SKIPped when
#                             playerctl or mpv-mpris is not installed).
#                             Also checks the saved resume queue with no player up.
#   scripts/smoke.sh --mutating
#                             OPT-IN account mutations, all reversible: the
#                             full throwaway-playlist lifecycle (create, add,
#                             duplicate, edit, move, remove, delete, cleanup)
#                             and a like+unlike round-trip on a track that was
#                             not already liked. Never touches an existing
#                             playlist. Also a save/remove round-trip on a
#                             known album, restoring its original library
#                             state.
#
# Commands SKIPPED unless --mutating (with or without --full) and why:
#   login                    interactive browser authentication
#   create-playlist          would leave a stray playlist on the account
#   playlist-add             mutates an existing playlist (incl. Liked Music)
#   playlist-add-items       mutates an existing playlist
#   playlist-edit            renames/re-privacy an existing playlist
#   playlist-delete          deletes a playlist
#   playlist-move            reorders an existing playlist's tracks
#   remove                   deletes entries from a playlist
#   like / dislike / unlike  mutates the account's liked songs
#   album-save / album-remove  mutates the account's saved-albums list
#   daemon / watch           foreground loops that never return; the same code
#                            path is covered by ensure-daemon + daemon-stop
#
# Notes on the default (non --full) run:
#   * The metadata response cache is exercised read-only: a freshly cleared
#     cache gives a live miss, the repeat call a hit (identical payload apart
#     from cached/stale), `-r` / `--refresh` a bypass, and every record must be
#     0700 / 0600. The stale offline fallback is checked by ageing the stored
#     ts and proxying one fetch at a dead port; the backend's expected
#     "YouTube session expired..." line on stderr is ignored there. Only the
#     plugin's own ~/.local/state/yt-music/cache is cleared, never user data.
#   * The artist lookup is also fetched live (`-r`) once so the assertion on
#     its non-empty "similar" artists list cannot be fooled by an old cache.
#   * Playback-affecting commands (play, pause, toggle, next, prev, seek,
#     seek-pct, volume, loop, shuffle, mix, radio, queue, queue-*, queue-add,
#     play-next, enqueue, enqueue-files, thumbnail, daemon, ensure-daemon,
#     daemon-stop, watch) are only run with --full; a single NOTE lists them.
#   * `enqueue` / `enqueue-files` call ensure_daemon() before their usage
#     check, so afterwards the status daemon is stopped again when this run is
#     what started it.
#   * `volume abc` only reaches its argument error while a player is up
#     (cmd_volume returns early with exit 0 when mpv is not running), so a
#     silent idle mpv is started on the plugin's IPC socket for the
#     usage-error section when none is running. No audio is played, and it is
#     stopped right after. If mpv cannot be started the check is SKIPped.
#
# queue-jump / queue-remove / queue-move need a live queue (>1 entry), so they
# only run under --full after play + queue-add; there a clean JSON ok:false
# (e.g. empty queue) counts as PASS, only a crash / non-JSON output is a FAIL.

set -uo pipefail

CTL="${YT_MUSIC_CTL:-$HOME/.local/bin/yt-music-ctl}"
CACHE_DIR="$HOME/.local/state/yt-music/cache"

# Known-good ids used across the checks.
LYRICS_VID="HUskuj8I9xY"
NO_LYRICS_VID="dQw4w9WgXcQ"
ALBUM_ID="MPREb_OWT3CzGkUeU"
ARTIST_ID="MPLAUCCzULu3prrEaPvM2ZtkJlYQ"
PLAYLIST_ID="PLZk4b3I85c8NQaGzPvC4LuRn-KQPxg9a7"

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
MPV_SOCKET="$RUNTIME_DIR/yt-music/mpv.sock"

FULL=0
MUTATING=0
for arg in "$@"; do
    case "$arg" in
        --full) FULL=1 ;;
        --mutating) MUTATING=1 ;;
        -h|--help)
            sed -n '2,61p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            printf 'smoke.sh: unknown option: %s\n' "$arg" >&2
            printf 'usage: scripts/smoke.sh [--full] [--mutating]\n' >&2
            exit 2
            ;;
    esac
done

PASS=0
FAIL=0
ERR_FILE=$(mktemp)
IDLE_MPV_PID=""

section() { printf '\n== %s ==\n' "$1"; }
pass() { PASS=$((PASS + 1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL %s%s\n' "$1" "${2:+ ($2)}"; }
skip() { printf 'SKIP %s - %s\n' "$1" "$2"; }

snippet() {
    local text=$1
    text=${text//$'\n'/ }
    text=${text//$'\t'/ }
    if ((${#text} > 160)); then
        text="${text:0:157}..."
    fi
    printf '%s' "$text"
}

detail() {
    if [[ -n $1 ]]; then
        snippet "$1"
    elif [[ -n ${2:-} ]]; then
        snippet "$2"
    else
        printf 'no output'
    fi
}

# check_ok <label> <cmd...> - stdout must be JSON with ok == true.
check_ok() {
    local label=$1 out rc err ok
    shift
    out=$("$@" 2>"$ERR_FILE")
    rc=$?
    err=$(cat "$ERR_FILE")
    ok=$(printf '%s' "$out" | python3 -c 'import sys, json
try:
    print("True" if json.load(sys.stdin).get("ok") is True else "False")
except Exception:
    print("False")' 2>/dev/null)
    if [[ $ok == True ]]; then
        pass "$label"
    else
        fail "$label" "exit $rc: $(detail "$out" "$err")"
    fi
}

# check_field <label> <expected-json> <field> <cmd...> - stdout JSON's <field>
# must equal <expected-json>. Use for booleans/numbers/strings.
check_field() {
    local label=$1 want=$2 field=$3 out rc got
    shift 3
    out=$("$@" 2>"$ERR_FILE")
    rc=$?
    got=$(printf '%s' "$out" | python3 -c "import sys, json
try:
    v = json.load(sys.stdin).get('$field')
    print(json.dumps(v))
except Exception:
    print('null')" 2>/dev/null)
    if [[ $got == "$want" ]]; then
        pass "$label"
    else
        fail "$label" "got $got want $want: $(detail "$out" "$(cat "$ERR_FILE")")"
    fi
}

# check_similar <label> <cmd...> - stdout JSON must have a non-empty "similar"
# list whose first entry has a non-empty browseId.
check_similar() {
    local label=$1 out rc ok
    shift
    out=$("$@" 2>"$ERR_FILE")
    rc=$?
    ok=$(printf '%s' "$out" | python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
    s = d.get("similar") or []
    print("True" if s and (s[0] or {}).get("browseId") else "False")
except Exception:
    print("False")' 2>/dev/null)
    if [[ $ok == True ]]; then
        pass "$label"
    else
        fail "$label" "exit $rc: $(detail "$out" "$(cat "$ERR_FILE")")"
    fi
}

# check_setvideoid <label> <cmd...> - stdout JSON's first track row must carry
# a non-empty setVideoId (needed for playlist reorder).
check_setvideoid() {
    local label=$1 out rc ok
    shift
    out=$("$@" 2>"$ERR_FILE")
    rc=$?
    ok=$(printf '%s' "$out" | python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
    t = d.get("tracks") or []
    print("True" if t and (t[0] or {}).get("setVideoId") else "False")
except Exception:
    print("False")' 2>/dev/null)
    if [[ $ok == True ]]; then
        pass "$label"
    else
        fail "$label" "exit $rc: $(detail "$out" "$(cat "$ERR_FILE")")"
    fi
}

# check_json <label> <cmd...> - stdout must be parseable JSON; ok may be false
# (used for queue-jump/queue-remove/queue-move where an empty queue is valid).
check_json() {
    local label=$1 out rc err
    shift
    out=$("$@" 2>"$ERR_FILE")
    rc=$?
    err=$(cat "$ERR_FILE")
    if printf '%s' "$out" | python3 -c 'import sys, json; json.load(sys.stdin)' 2>/dev/null; then
        pass "$label"
    else
        fail "$label" "exit $rc: $(detail "$out" "$err")"
    fi
}

# check_exit0 <label> <cmd...> - just assert exit code 0 (status / toggle /
# thumbnail print nothing on success).
check_exit0() {
    local label=$1 out rc
    shift
    out=$("$@" 2>"$ERR_FILE")
    rc=$?
    if [[ $rc -eq 0 ]]; then
        pass "$label"
    else
        fail "$label" "exit $rc: $(detail "$out" "$(cat "$ERR_FILE")")"
    fi
}

# check_usage <label> <cmd...> - exit code must be 1 and stdout must NOT be a
# JSON success payload (usage errors are printed to stderr).
check_usage() {
    local label=$1 out rc err
    shift
    out=$("$@" 2>"$ERR_FILE")
    rc=$?
    err=$(cat "$ERR_FILE")
    if [[ $rc -ne 1 ]]; then
        fail "$label" "exit $rc, want 1: $(detail "$out" "$err")"
        return
    fi
    if printf '%s' "$out" | python3 -c 'import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
    sys.exit(0 if isinstance(data, dict) and data.get("ok") is True else 1)' 2>/dev/null; then
        fail "$label" "unexpected JSON success on stdout"
        return
    fi
    pass "$label"
}

# check_cached <label> <True|False> <cmd...> - exit 0 and stdout must be JSON
# with ok == true and the top-level .cached flag exactly <True|False>.
check_cached() {
    local label=$1 want=$2 out rc err got
    shift 2
    out=$("$@" 2>"$ERR_FILE")
    rc=$?
    err=$(cat "$ERR_FILE")
    got=$(printf '%s' "$out" | python3 -c 'import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    print("?"); raise SystemExit
flags = {True: "True", False: "False"}
print(flags.get(data.get("cached"), "?") if data.get("ok") is True else "?")' 2>/dev/null)
    if [[ $rc -eq 0 && $got == "$want" ]]; then
        pass "$label"
    else
        fail "$label" "exit $rc: $(detail "$out" "$err")"
    fi
}

# check_cache_hit <label> <cmd...> - run the command twice: the first reply
# must be a live fetch (.cached false) and the second a cache hit (.cached
# true), with both payloads identical once cached/stale are removed.
check_cache_hit() {
    local label=$1 f1 f2 rc1 rc2 err1 verdict
    shift
    f1=$(mktemp)
    f2=$(mktemp)
    "$@" >"$f1" 2>"$ERR_FILE"
    rc1=$?
    err1=$(cat "$ERR_FILE")
    "$@" >"$f2" 2>"$ERR_FILE"
    rc2=$?
    verdict=$(python3 - "$f1" "$f2" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        first = json.load(fh)
    with open(sys.argv[2]) as fh:
        second = json.load(fh)
except Exception as exc:
    print("not JSON: %s" % exc)
    raise SystemExit
if first.get("cached") is not False:
    print("run1 cached=%r, want False" % (first.get("cached"),))
    raise SystemExit
if second.get("cached") is not True:
    print("run2 cached=%r, want True" % (second.get("cached"),))
    raise SystemExit
for record in (first, second):
    record.pop("cached", None)
    record.pop("stale", None)
if first != second:
    keys = sorted(set(first) ^ set(second))
    print("payloads differ (%s)" % (", ".join(keys) or "same keys, other values"))
    raise SystemExit
print("ok")
PY
)
    rm -f "$f1" "$f2"
    if [[ $verdict == ok ]]; then
        pass "$label"
    else
        fail "$label" "exit $rc1/$rc2: $(detail "$verdict" "$err1")"
    fi
}

# check_stale <label> <cmd...> - exit 0 with a cached, stale JSON payload: the
# live fetch failed and the aged entry was served instead. The failure the
# backend reports on stderr in that case is expected and ignored.
check_stale() {
    local label=$1 out rc err
    shift
    out=$("$@" 2>"$ERR_FILE")
    rc=$?
    err=$(cat "$ERR_FILE")
    if [[ $rc -eq 0 ]] && printf '%s' "$out" | python3 -c 'import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if data.get("ok") is True and data.get("cached") is True
         and data.get("stale") is True else 1)' 2>/dev/null; then
        pass "$label"
    else
        fail "$label" "exit $rc: $(detail "$out" "$err")"
    fi
}

# check_not_ok <label> <error> <cmd...> - exit 0 and stdout must be JSON with
# ok == false; a non-empty <error> also pins the .error text. Invalid ids and
# "nothing playing" are clean JSON answers, not crashes.
check_not_ok() {
    local label=$1 message=$2 out rc got
    shift 2
    out=$("$@" 2>"$ERR_FILE")
    rc=$?
    got=$(printf '%s' "$out" | python3 -c 'import sys, json
want = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    print("?"); raise SystemExit
if data.get("ok") is not False:
    print("?")
elif want and data.get("error") != want:
    print("error=%r" % (data.get("error"),))
else:
    print("True")' "$message" 2>/dev/null)
    if [[ $rc -eq 0 && $got == True ]]; then
        pass "$label"
    else
        fail "$label" "exit $rc: $(detail "$out" "$(cat "$ERR_FILE")")"
    fi
}

# check_json_pred <label> <predicate> <cmd...> - stdout must be JSON and the
# python <predicate>, evaluated with `d` bound to the parsed payload, must
# print True. Use when a reply needs a shape check that no single field has.
check_json_pred() {
    local label=$1 pred=$2 out rc got
    shift 2
    out=$("$@" 2>"$ERR_FILE")
    rc=$?
    got=$(printf '%s' "$out" | python3 -c "import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('?'); raise SystemExit
print('True' if ($pred) else 'False')" 2>/dev/null)
    if [[ $rc -eq 0 && $got == True ]]; then
        pass "$label"
    else
        fail "$label" "exit $rc: $(detail "$out" "$(cat "$ERR_FILE")")"
    fi
}

# check_precache <label> <cmd...> - a successful precache: exit 0, JSON ok with
# a non-empty path whose file exists and is mode 600.
check_precache() {
    local label=$1 out rc err path mode
    shift
    out=$("$@" 2>"$ERR_FILE")
    rc=$?
    err=$(cat "$ERR_FILE")
    path=$(printf '%s' "$out" | python3 -c 'import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit
print(data.get("path") or "" if data.get("ok") is True else "")' 2>/dev/null)
    if [[ -z $path ]]; then
        fail "$label" "exit $rc: $(detail "$out" "$err")"
        return
    fi
    if [[ ! -f $path ]]; then
        fail "$label" "missing file $path"
        return
    fi
    mode=$(stat -c %a "$path" 2>/dev/null)
    if [[ $mode != 600 ]]; then
        fail "$label" "mode $mode, want 600: $path"
        return
    fi
    pass "$label"
}

# check_mode <label> <path> <mode> - stat -c %a must report <mode>.
check_mode() {
    local label=$1 path=$2 want=$3 mode
    mode=$(stat -c %a "$path" 2>/dev/null)
    if [[ $mode == "$want" ]]; then
        pass "$label"
    else
        fail "$label" "${mode:-missing} ($path)"
    fi
}

# check_cache_records - every *.json record in the metadata cache is 0600.
check_cache_records() {
    local path mode seen=0 bad=""
    for path in "$CACHE_DIR"/*.json; do
        [[ -e $path ]] || continue
        seen=$((seen + 1))
        mode=$(stat -c %a "$path" 2>/dev/null)
        if [[ $mode != 600 ]]; then
            bad+="$path=$mode "
        fi
    done
    if ((seen == 0)); then
        fail "cache records mode 600" "no *.json under $CACHE_DIR"
    elif [[ -n $bad ]]; then
        fail "cache records mode 600" "$bad"
    else
        pass "cache records mode 600"
    fi
}

# age_cache - push every stored record's ts ten years into the past so its TTL
# reads as expired; the payloads themselves are left untouched.
age_cache() {
    python3 - "$CACHE_DIR" <<'PY'
import json, os, sys, time
cache_dir = sys.argv[1]
if not os.path.isdir(cache_dir):
    raise SystemExit
for name in sorted(os.listdir(cache_dir)):
    if not name.endswith(".json"):
        continue
    path = os.path.join(cache_dir, name)
    try:
        with open(path) as fh:
            record = json.load(fh)
        if not isinstance(record, dict):
            continue
        record["ts"] = time.time() - 10 * 365 * 24 * 3600
        with open(path, "w") as fh:
            json.dump(record, fh)
        os.chmod(path, 0o600)
    except Exception:
        pass
PY
}

mpv_alive() {
    python3 - "$MPV_SOCKET" <<'PY' 2>/dev/null
import json, os, socket, sys
path = sys.argv[1]
if not os.path.exists(path):
    sys.exit(1)
try:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1.5)
    sock.connect(path)
    sock.sendall(b'{"command":["get_property","mpv-version"]}\n')
    reply = json.loads(sock.recv(4096).split(b"\n")[0])
    sock.close()
except Exception:
    sys.exit(1)
sys.exit(0 if reply.get("error") == "success" else 1)
PY
}

start_idle_mpv() {
    mpv_alive && return 0
    command -v mpv >/dev/null 2>&1 || return 1
    mkdir -p "$RUNTIME_DIR/yt-music" 2>/dev/null
    chmod 700 "$RUNTIME_DIR/yt-music" 2>/dev/null
    mpv --idle=yes --no-video --really-quiet \
        --input-ipc-server="$MPV_SOCKET" >/dev/null 2>&1 &
    IDLE_MPV_PID=$!
    local i
    for i in $(seq 1 40); do
        mpv_alive && return 0
        kill -0 "$IDLE_MPV_PID" 2>/dev/null || return 1
        sleep 0.2
    done
    return 1
}

stop_idle_mpv() {
    [[ -n $IDLE_MPV_PID ]] || return 0
    kill "$IDLE_MPV_PID" 2>/dev/null
    wait "$IDLE_MPV_PID" 2>/dev/null
    IDLE_MPV_PID=""
    # Drop the IPC socket we created when it went stale with us, so the run
    # leaves nothing behind (the backend also unlinks it before spawning mpv).
    if [[ -S $MPV_SOCKET ]] && ! mpv_alive; then
        rm -f "$MPV_SOCKET"
    fi
    return 0
}

# daemon_running - the same identity check the backend uses: the pid file must
# name a live process with a matching start time and executable. (A pgrep -f
# pattern would also match this script's own / its caller's command line.)
daemon_running() {
    python3 - <<'PY'
import json, os, sys
path = os.path.expanduser("~/.local/state/yt-music/daemon.pid")
try:
    with open(path) as fh:
        record = json.load(fh)
except Exception:
    sys.exit(1)
if not isinstance(record, dict):
    sys.exit(1)
pid = record.get("pid")
if (not isinstance(pid, int) or pid <= 1 or not record.get("start_time")
        or not record.get("executable")):
    sys.exit(1)
try:
    with open("/proc/%d/stat" % pid) as fh:
        stat_data = fh.read()
    start_time = stat_data[stat_data.rfind(")") + 2:].split()[19]
    executable = os.path.realpath(os.readlink("/proc/%d/exe" % pid))
except (OSError, IndexError):
    sys.exit(1)
sys.exit(0 if (start_time == str(record.get("start_time"))
               and executable == str(record.get("executable"))) else 1)
PY
}

cleanup() {
    stop_idle_mpv
    rm -f "$ERR_FILE"
}
trap cleanup EXIT

if [[ ! -x $CTL ]]; then
    printf 'smoke.sh: ctl not executable: %s\n' "$CTL" >&2
    exit 2
fi

DAEMON_BEFORE=0
daemon_running && DAEMON_BEFORE=1

# check_playlist_counts - every playlist whose description advertises "N tracks"
# must report count N; auto playlists without a count are allowed to be null and
# count must never be a non-integer.
check_playlist_counts() {
    local out rc got
    out=$("$CTL" playlists 2>"$ERR_FILE")
    rc=$?
    got=$(printf '%s' "$out" | python3 -c 'import sys, json, re
try:
    data = json.load(sys.stdin)
except Exception:
    print("parse"); raise SystemExit
if data.get("ok") is not True:
    print("not-ok"); raise SystemExit
for pl in data.get("playlists", []):
    m = re.search(r"(\d+) tracks?", pl.get("description") or "")
    c = pl.get("count")
    if c is not None and not isinstance(c, int):
        print("count-not-int: %r" % (c,)); raise SystemExit
    if m and c is not None and int(m.group(1)) != c:
        print("mismatch %s: desc=%s count=%s" % (pl.get("title"), m.group(1), c))
        raise SystemExit
print("ok")' 2>/dev/null)
    if [[ $rc -eq 0 && $got == ok ]]; then
        pass "playlists count matches description"
    else
        fail "playlists count matches description" "${got:-no output}"
    fi
}

# check_queue_remove_key - remove the last queue entry by its row key and assert
# the count drops by exactly one (covers the key-based batch removal path).
check_queue_remove_key() {
    local label=$1 before after key out rc
    before=$("$CTL" queue-list 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("count",0))' 2>/dev/null)
    key=$("$CTL" queue-list 2>/dev/null | python3 -c '
import sys, json
d = json.load(sys.stdin)
seen = {}
rows = []
for t in d.get("tracks", []):
    v = t.get("videoId", "")
    if v:
        o = seen.get(v, 0); seen[v] = o + 1
        rows.append("v:%s#%d" % (v, o))
    else:
        rows.append("q:%d" % t.get("index", -1))
print(rows[-1] if rows else "")' 2>/dev/null)
    if [[ -z $key ]]; then
        fail "$label" "empty queue"
        return
    fi
    out=$("$CTL" queue-remove-keys "$key" 2>"$ERR_FILE"); rc=$?
    after=$("$CTL" queue-list 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("count",0))' 2>/dev/null)
    if [[ $rc -eq 0 && $after -eq $((before - 1)) ]]; then
        pass "$label"
    else
        fail "$label" "key=$key before=$before after=$after: $(detail "$out" "$(cat "$ERR_FILE")")"
    fi
}

# check_queue_saved - the resumable queue answers while no player is up:
# queue-list falls back to ~/.local/state/yt-music/session.json and
# queue-remove-keys / queue-clear edit that snapshot instead of mpv's playlist.
# Silent: never starts a player. Any existing session is copied aside first and
# put back at the end; a run with no session removes the one this writes.
check_queue_saved() {
    local session="$HOME/.local/state/yt-music/session.json"
    local bak="" had=0

    if [[ -f $session ]]; then
        bak=$(mktemp /tmp/yt-music-session.XXXXXX)
        if cp -f "$session" "$bak"; then
            had=1
        else
            rm -f "$bak"
            bak=""
        fi
    fi
    printf '{"videoIds":["%s","%s"],"index":0,"position":0}\n' \
        "$LYRICS_VID" "$NO_LYRICS_VID" >"$session"
    chmod 600 "$session"

    check_field "saved queue-list count" 2 "count" "$CTL" queue-list
    check_field "saved queue-list saved" true "saved" "$CTL" queue-list
    check_field "saved queue-list not playing" false "playing" "$CTL" queue-list
    check_json_pred "saved queue-remove-keys removes one" \
        'd.get("ok") is True and len(d.get("removed") or []) == 1' \
        "$CTL" queue-remove-keys "v:$NO_LYRICS_VID#0"
    check_field "saved queue-list count after remove" 1 "count" "$CTL" queue-list
    check_ok "saved queue-clear" "$CTL" queue-clear
    check_field "saved queue-list count after clear" 0 "count" "$CTL" queue-list
    check_field "saved queue-list saved after clear" false "saved" "$CTL" queue-list

    if [[ $had -eq 1 ]]; then
        mv -f "$bak" "$session"
    else
        rm -f "$session"
    fi
}

# check_thumbnail_cached - a second thumbnail call must not rewrite the file.
check_thumbnail_cached() {
    local label=$1 vid=$2 path m1 m2
    path="$HOME/.cache/yt-music/thumbs/$vid.jpg"
    "$CTL" thumbnail "$vid" >/dev/null 2>&1
    if [[ ! -f $path ]]; then
        fail "$label" "no cached thumbnail at $path"
        return
    fi
    m1=$(stat -c %Y "$path")
    sleep 1
    "$CTL" thumbnail "$vid" >/dev/null 2>&1
    m2=$(stat -c %Y "$path")
    if [[ $m1 == "$m2" ]]; then
        pass "$label"
    else
        fail "$label" "re-downloaded (mtime changed)"
    fi
}

# ctl_status_paused - run `yt-music-ctl status` and echo the paused flag from
# status.json as True/False; empty when the file cannot be read.
ctl_status_paused() {
    "$CTL" status >/dev/null 2>&1
    python3 - "$HOME/.local/state/yt-music/status.json" <<'PY'
import json, sys
try:
    paused = json.load(open(sys.argv[1])).get("paused")
except Exception:
    sys.exit(0)
print("True" if paused else "False")
PY
}

# check_mpris - playback must be reachable over D-Bus as `mpv`, which is how
# the desktop media keys and Omarchy's media widget drive this plugin. Call it
# while a track is playing. SKIPs cleanly when playerctl or the mpv-mpris
# script is missing; otherwise asserts the player, its title and art metadata,
# and that an MPRIS play-pause round-trip shows up in `yt-music-ctl status`.
check_mpris() {
    local script="" title="" art="" before="" after="" i
    if ! command -v playerctl >/dev/null 2>&1; then
        skip "mpris" "playerctl is not installed"
        return
    fi
    for script in /etc/mpv/scripts/mpris.so /usr/lib/mpv-mpris/mpris.so \
                  "$HOME/.config/mpv/scripts/mpris.so"; do
        if [[ -e $script ]]; then
            break
        fi
        script=""
    done
    if [[ -z $script ]]; then
        skip "mpris" "mpv-mpris is not installed"
        return
    fi

    # The D-Bus name appears once mpv has loaded the script.
    for i in $(seq 1 25); do
        if playerctl -l 2>/dev/null | grep -qE '^mpv(\.|$)'; then
            break
        fi
        sleep 0.2
    done
    if playerctl -l 2>/dev/null | grep -qE '^mpv(\.|$)'; then
        pass "mpris player listed by playerctl"
    else
        fail "mpris player listed by playerctl" "$(snippet "$(playerctl -l 2>&1)")"
        return
    fi

    # Title and cover art land a moment after the stream starts.
    for i in $(seq 1 25); do
        title=$(playerctl -p mpv metadata xesam:title 2>/dev/null) || title=""
        art=$(playerctl -p mpv metadata mpris:artUrl 2>/dev/null) || art=""
        [[ -n $title && -n $art ]] && break
        sleep 0.2
    done
    if [[ -n $title ]]; then
        pass "mpris xesam:title non-empty"
    else
        fail "mpris xesam:title non-empty" "empty"
    fi
    if [[ -n $art ]]; then
        pass "mpris mpris:artUrl non-empty"
    else
        fail "mpris mpris:artUrl non-empty" "empty"
    fi

    before=$(ctl_status_paused)
    if [[ -z $before ]]; then
        fail "mpris play-pause flips status" "status.json unreadable"
        return
    fi
    playerctl -p mpv play-pause >/dev/null 2>&1
    after=""
    for i in $(seq 1 25); do
        after=$(ctl_status_paused)
        [[ -n $after && $after != "$before" ]] && break
        sleep 0.2
    done
    if [[ -n $after && $after != "$before" ]]; then
        pass "mpris play-pause flips status ($before -> $after)"
    else
        fail "mpris play-pause flips status" \
             "expected a flip from $before, got ${after:-unreadable}"
        return
    fi
    playerctl -p mpv play-pause >/dev/null 2>&1
    after=""
    for i in $(seq 1 25); do
        after=$(ctl_status_paused)
        [[ -n $after && $after == "$before" ]] && break
        sleep 0.2
    done
    if [[ -n $after && $after == "$before" ]]; then
        pass "mpris play-pause restores status ($after)"
    else
        fail "mpris play-pause restores status" \
             "expected $before, got ${after:-unreadable}"
    fi
}

# --- opt-in, fully reversible account mutations (--mutating) -----------------
mutating_playlist() {
    local name="ZZ-SMOKE-DELETE-ME-$$" id out rc
    out=$("$CTL" create-playlist "$name" 2>"$ERR_FILE"); rc=$?
    id=$(printf '%s' "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
    if [[ $rc -eq 0 && -n $id ]]; then
        pass "mutating create-playlist"
    else
        fail "mutating create-playlist" "$(detail "$out" "$(cat "$ERR_FILE")")"
        return
    fi

    out=$("$CTL" playlist-add-items "$id" "v:$LYRICS_VID" "v:$NO_LYRICS_VID" 2>"$ERR_FILE"); rc=$?
    if [[ $rc -eq 0 ]] && printf '%s' "$out" | grep -q '"added": 2'; then
        pass "mutating playlist-add-items"
    else
        fail "mutating playlist-add-items" "$(detail "$out" "$(cat "$ERR_FILE")")"
    fi

    out=$("$CTL" playlist-add-items "$id" "v:$LYRICS_VID" 2>"$ERR_FILE")
    if printf '%s' "$out" | grep -q '"duplicates": 1'; then
        pass "mutating playlist-add-items duplicate"
    else
        fail "mutating playlist-add-items duplicate" "$(detail "$out" "")"
    fi

    out=$("$CTL" playlist-edit "$id" --title "$name-renamed" --privacy UNLISTED 2>"$ERR_FILE"); rc=$?
    if [[ $rc -eq 0 ]] && printf '%s' "$out" | grep -q '"ok": true'; then
        pass "mutating playlist-edit"
    else
        fail "mutating playlist-edit" "$(detail "$out" "$(cat "$ERR_FILE")")"
    fi

    out=$("$CTL" playlist-move "$id" 0 1 2>"$ERR_FILE"); rc=$?
    if [[ $rc -eq 0 ]] && printf '%s' "$out" | grep -q '"ok": true'; then
        pass "mutating playlist-move"
    else
        fail "mutating playlist-move" "$(detail "$out" "$(cat "$ERR_FILE")")"
    fi

    out=$("$CTL" remove "$id" "$LYRICS_VID" 2>"$ERR_FILE"); rc=$?
    if [[ $rc -eq 0 ]] && printf '%s' "$out" | grep -q '"removed": 1'; then
        pass "mutating remove"
    else
        fail "mutating remove" "$(detail "$out" "$(cat "$ERR_FILE")")"
    fi

    out=$("$CTL" playlist-delete "$id" 2>"$ERR_FILE"); rc=$?
    if [[ $rc -eq 0 ]] && printf '%s' "$out" | grep -q '"deleted": true'; then
        pass "mutating playlist-delete"
    else
        fail "mutating playlist-delete" "$(detail "$out" "$(cat "$ERR_FILE")")"
    fi

    if "$CTL" playlists 2>/dev/null | grep -q "$id"; then
        fail "mutating cleanup" "throwaway playlist $id still present"
    else
        pass "mutating cleanup (throwaway gone)"
    fi
}

mutating_like() {
    local before after id out rc
    id=$("$CTL" search -f songs "smoke test" 2>/dev/null | python3 -c 'import sys,json
d = json.load(sys.stdin)
items = d.get("items") or []
print(items[0].get("videoId","") if items else "")' 2>/dev/null)
    if [[ -z $id ]]; then
        skip "mutating like/unlike" "no search result videoId"
        return
    fi
    if "$CTL" liked -r 2>/dev/null | grep -q "$id"; then
        skip "mutating like/unlike" "$id is already liked; refusing to touch it"
        return
    fi
    before=$("$CTL" liked -r 2>/dev/null | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("items",[])))' 2>/dev/null)
    out=$("$CTL" like "$id" 2>"$ERR_FILE"); rc=$?
    after=$("$CTL" liked -r 2>/dev/null | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("items",[])))' 2>/dev/null)
    if [[ $rc -eq 0 ]] && printf '%s' "$out" | grep -q '"liked": true' && [[ $after -eq $((before + 1)) ]]; then
        pass "mutating like"
    else
        fail "mutating like" "$(detail "$out" "$(cat "$ERR_FILE")") before=$before after=$after"
    fi
    out=$("$CTL" unlike "$id" 2>"$ERR_FILE"); rc=$?
    after=$("$CTL" liked -r 2>/dev/null | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("items",[])))' 2>/dev/null)
    if [[ $rc -eq 0 ]] && [[ $after -eq $before ]]; then
        pass "mutating unlike (restored)"
    else
        fail "mutating unlike (restored)" "$(detail "$out" "$(cat "$ERR_FILE")") before=$before after=$after"
    fi
}

# Save/remove round-trip for one album, restoring whatever state it started in.
# Silent: rating an album never touches playback.
mutating_album() {
    local initial out rc
    out=$("$CTL" album-status "$ALBUM_ID" 2>"$ERR_FILE"); rc=$?
    initial=$(printf '%s' "$out" | python3 -c 'import sys, json
try:
    d = json.load(sys.stdin)
    print("True" if d.get("ok") is True and d.get("inLibrary") is True
          else ("False" if d.get("ok") is True else "?"))
except Exception:
    print("?")' 2>/dev/null)
    if [[ $rc -eq 0 && $initial != "?" ]]; then
        pass "mutating album-status (initially saved: $initial)"
    else
        fail "mutating album-status" "$(detail "$out" "$(cat "$ERR_FILE")")"
        return
    fi

    # Start from "not saved" so album-save itself is exercised.
    if [[ $initial == True ]]; then
        out=$("$CTL" album-remove "$ALBUM_ID" 2>"$ERR_FILE"); rc=$?
        if [[ $rc -eq 0 ]] && printf '%s' "$out" | grep -q '"saved": false'; then
            pass "mutating album-remove (pre-clean)"
        else
            fail "mutating album-remove (pre-clean)" "$(detail "$out" "$(cat "$ERR_FILE")")"
        fi
    fi

    out=$("$CTL" album-save "$ALBUM_ID" 2>"$ERR_FILE"); rc=$?
    if [[ $rc -eq 0 ]] && printf '%s' "$out" | grep -q '"saved": true'; then
        pass "mutating album-save"
    else
        fail "mutating album-save" "$(detail "$out" "$(cat "$ERR_FILE")")"
    fi
    check_field "mutating album-status after save" true "inLibrary" \
        "$CTL" album-status "$ALBUM_ID"
    # album-save drops the cached `library albums` list, so this is live.
    check_json_pred "mutating library albums contains $ALBUM_ID" \
        "any(i.get('browseId') == '$ALBUM_ID' for i in (d.get('items') or []))" \
        "$CTL" library albums 200

    out=$("$CTL" album-remove "$ALBUM_ID" 2>"$ERR_FILE"); rc=$?
    if [[ $rc -eq 0 ]] && printf '%s' "$out" | grep -q '"saved": false'; then
        pass "mutating album-remove"
    else
        fail "mutating album-remove" "$(detail "$out" "$(cat "$ERR_FILE")")"
    fi
    check_field "mutating album-status after remove" false "inLibrary" \
        "$CTL" album-status "$ALBUM_ID"

    # Restore the state the album had before this run.
    if [[ $initial == True ]]; then
        out=$("$CTL" album-save "$ALBUM_ID" 2>"$ERR_FILE"); rc=$?
        if [[ $rc -eq 0 ]] && printf '%s' "$out" | grep -q '"saved": true'; then
            pass "mutating album save (restored)"
        else
            fail "mutating album save (restored)" "$(detail "$out" "$(cat "$ERR_FILE")")"
        fi
    fi
}

# ---------------------------------------------------------------- read-only
section "read-only"
check_ok "playlists" "$CTL" playlists
check_playlist_counts
check_exit0 "status" "$CTL" status
check_ok "search songs" "$CTL" search -f songs fleetwood mac
check_ok "search albums" "$CTL" search -f albums fleetwood mac
check_ok "search artists" "$CTL" search -f artists fleetwood mac
check_ok "search playlists" "$CTL" search -f playlists fleetwood mac
check_ok "home 2" "$CTL" home 2
check_ok "history 5" "$CTL" history 5
check_ok "library songs 5" "$CTL" library songs 5
check_ok "library artists 5" "$CTL" library artists 5
check_ok "library albums 5" "$CTL" library albums 5
check_ok "library playlists 5" "$CTL" library playlists 5
check_ok "liked 5" "$CTL" liked 5
check_ok "lyrics $LYRICS_VID" "$CTL" lyrics "$LYRICS_VID"
check_ok "queue-list" "$CTL" queue-list

# ---------------------------------------------------------------- navigation
section "navigation (network)"
check_ok "album $ALBUM_ID" "$CTL" album "$ALBUM_ID"
check_ok "artist $ARTIST_ID" "$CTL" artist "$ARTIST_ID"
check_similar "artist $ARTIST_ID similar artists" "$CTL" artist -r "$ARTIST_ID"
check_ok "playlist $PLAYLIST_ID" "$CTL" playlist "$PLAYLIST_ID"
# Fetched live so the assertion cannot be fooled by a pre-setVideoId cache
# record; every emitted track row must carry one for playlist reorder.
check_setvideoid "playlist $PLAYLIST_ID first track setVideoId" \
    "$CTL" playlist -r "$PLAYLIST_ID"

# ------------------------------------------------------- metadata cache (G5)
section "cache (read-only)"
# Start from an empty cache so the first call of every pair below is provably
# live; only the plugin's own metadata cache is cleared, never user data.
rm -rf "$CACHE_DIR"
check_cache_hit "album $ALBUM_ID (miss then hit)" "$CTL" album "$ALBUM_ID"
check_cache_hit "artist $ARTIST_ID (miss then hit)" "$CTL" artist "$ARTIST_ID"
check_cache_hit "search -f albums fleetwood mac (miss then hit)" \
    "$CTL" search -f albums fleetwood mac
check_cache_hit "lyrics $LYRICS_VID (miss then hit)" "$CTL" lyrics "$LYRICS_VID"
check_cache_hit "liked (miss then hit)" "$CTL" liked 200
check_cached "album -r bypasses a fresh cache" False "$CTL" album "$ALBUM_ID" -r
check_cached "liked -r bypasses a fresh cache" False "$CTL" liked 200 -r
check_mode "cache dir mode 700" "$CACHE_DIR" 700
check_cache_records
# Offline fallback without cutting the real network: age every record, then
# send one fetch at a dead proxy. The backend answers from the cache and the
# "YouTube session expired..." line it prints on stderr is expected here.
age_cache
check_stale "album serves a stale record offline" \
    env HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 \
    "$CTL" album "$ALBUM_ID"
# Left stale on purpose: nothing after this depends on a fresh cache, and a
# later live fetch would simply rewrite the records.

# ---------------------------------------------------------------- usage errors
section "usage errors"
if start_idle_mpv; then
    if [[ -z $IDLE_MPV_PID ]]; then
        printf 'NOTE using the already-running mpv for usage-error checks\n'
    else
        printf 'NOTE started a silent idle mpv for usage-error checks\n'
    fi
else
    printf 'NOTE could not start idle mpv\n'
fi

check_usage "enqueue-files (no args)" "$CTL" enqueue-files
check_usage "enqueue-files bogus" "$CTL" enqueue-files bogus "$LYRICS_VID"
check_usage "enqueue (no args)" "$CTL" enqueue
check_usage "lyrics (no args)" "$CTL" lyrics
check_usage "lyrics bad id" "$CTL" lyrics 'bad##id'
check_usage "radio usage" "$CTL" radio
if [[ -n $IDLE_MPV_PID ]] || mpv_alive; then
    check_usage "volume abc" "$CTL" volume abc
    check_not_ok "loop banana (invalid mode)" "" "$CTL" loop banana
else
    skip "volume abc" "no player; cmd_volume returns 0 before validating args"
fi
check_usage "seek abc" "$CTL" seek abc
check_usage "seek-pct abc" "$CTL" seek-pct abc
check_usage "play malformed id" "$CTL" play short
check_usage "playlist-add-items (no args)" "$CTL" playlist-add-items
check_usage "playlist-add-items bad token" "$CTL" playlist-add-items "$PLAYLIST_ID" bogus-token
check_usage "playlist-edit (no args)" "$CTL" playlist-edit
check_usage "playlist-edit bad privacy" "$CTL" playlist-edit "$PLAYLIST_ID" --privacy SOMETIMES
check_usage "playlist-delete (no args)" "$CTL" playlist-delete
check_usage "playlist-move (no args)" "$CTL" playlist-move
check_usage "playlist-move bad index" "$CTL" playlist-move "$PLAYLIST_ID" abc 1

stop_idle_mpv

# enqueue/enqueue-files started the status daemon before failing; stop it again
# when this run is what brought it up.
if [[ $DAEMON_BEFORE -eq 0 ]] && daemon_running; then
    "$CTL" daemon-stop >/dev/null 2>&1
fi

# ---------------------------------------------------------------- skipped
section "skipped"
skip "login" "interactive browser authentication"
skip "create-playlist" "would create a stray playlist on the account"
skip "playlist-add" "mutates a playlist (incl. Liked Music)"
skip "playlist-add-items" "mutates a playlist"
skip "playlist-edit" "renames/re-privates a playlist"
skip "playlist-delete" "deletes a playlist"
skip "playlist-move" "reorders a playlist's tracks"
skip "remove" "deletes playlist entries"
skip "like / dislike / unlike" "mutates the account's liked songs"
skip "album-save / album-remove" "mutates the account's saved-albums list"
skip "daemon / watch" "foreground loop that never returns (use ensure-daemon)"

if [[ $MUTATING -eq 1 ]]; then
    section "mutating (--mutating)"
    printf 'NOTE account mutations are fully reversible (throwaway playlist; like+unlike; album save/remove)\n'
    mutating_playlist
    mutating_like
    mutating_album
fi

if [[ $FULL -eq 0 ]]; then
    printf 'NOTE playback-affecting commands skipped - pass --full: play pause '
    printf 'resume toggle next prev seek seek-pct volume loop shuffle mix radio queue '
    printf 'queue-jump queue-remove queue-move queue-add play-next enqueue '
    printf 'enqueue-files thumbnail ensure-daemon daemon-stop\n'
fi

# ---------------------------------------------------------------- playback
if [[ $FULL -eq 1 ]]; then
    section "playback (--full)"
    check_ok "ensure-daemon" "$CTL" ensure-daemon
    check_ok "play $LYRICS_VID" "$CTL" play "$LYRICS_VID"
    check_exit0 "status" "$CTL" status
    check_mpris
    check_ok "pause" "$CTL" pause
    check_ok "resume" "$CTL" resume
    check_exit0 "toggle" "$CTL" toggle
    check_ok "seek 5" "$CTL" seek 5
    check_ok "seek-pct 50" "$CTL" seek-pct 50
    check_ok "volume 70" "$CTL" volume 70
    check_ok "loop inf" "$CTL" loop inf
    check_ok "loop off" "$CTL" loop off
    check_ok "shuffle" "$CTL" shuffle
    check_ok "shuffle off" "$CTL" shuffle
    check_ok "queue-add $NO_LYRICS_VID" "$CTL" queue-add "$NO_LYRICS_VID"
    check_ok "queue-list" "$CTL" queue-list
    check_json "queue-jump 0" "$CTL" queue-jump 0
    check_json "queue-move 1 0" "$CTL" queue-move 1 0
    check_not_ok "queue-jump out of range" "Index out of range" "$CTL" queue-jump 999
    check_not_ok "queue-remove out of range" "Index out of range" "$CTL" queue-remove 999
    check_not_ok "queue-move out of range" "Index out of range" "$CTL" queue-move 999 0
    check_ok "queue-list after out-of-range (mpv survived)" "$CTL" queue-list
    check_json "queue-remove 1" "$CTL" queue-remove 1
    check_ok "play-next $NO_LYRICS_VID" "$CTL" play-next "$NO_LYRICS_VID"
    check_ok "enqueue queue album $ALBUM_ID" "$CTL" enqueue queue album "$ALBUM_ID"
    check_ok "enqueue-files queue $NO_LYRICS_VID" "$CTL" enqueue-files queue "$NO_LYRICS_VID"
    check_ok "next" "$CTL" next
    check_ok "prev" "$CTL" prev
    check_exit0 "thumbnail $LYRICS_VID" "$CTL" thumbnail "$LYRICS_VID"
    check_thumbnail_cached "thumbnail cache hit (no re-download)" "$LYRICS_VID"
    check_ok "mix $LYRICS_VID" "$CTL" mix "$LYRICS_VID"
    check_ok "queue $PLAYLIST_ID" "$CTL" queue "$PLAYLIST_ID"
    check_queue_remove_key "queue-remove-keys (key-based)"
    check_ok "queue-remove-keys bogus key (no-op)" "$CTL" queue-remove-keys "v:aaaaaaaaaaa#9"

    section "cleanup (--full)"
    check_ok "stop" "$CTL" stop
    check_ok "daemon-stop" "$CTL" daemon-stop

    # --------------------------------------------------------- radio (G5)
    # radio calls ensure_daemon() and starts mpv on every path (cache hit
    # included), so it only ever runs here, behind the --full guard, with the
    # player/daemon state pinned before and after.
    section "radio (network, --full only)"
    "$CTL" stop >/dev/null 2>&1; "$CTL" daemon-stop >/dev/null 2>&1; sleep 1
    check_ok "radio $ARTIST_ID" "$CTL" radio "$ARTIST_ID"
    check_field "radio $ARTIST_ID mix flag" true "mix" "$CTL" radio "$ARTIST_ID"
    check_cached "radio $ARTIST_ID (cache hit)" True "$CTL" radio "$ARTIST_ID"
    "$CTL" stop >/dev/null 2>&1; "$CTL" daemon-stop >/dev/null 2>&1

    # ------------------------------------------------------- precache (G5)
    # No `enqueue play album ...` queue-surgery check here on purpose: it is
    # too heavy and playback-affecting for a smoke script, and the queue order
    # it would verify was already checked by hand. The cleanup above stopped
    # mpv and the daemon; re-assert that so the "nothing playing" answer is
    # deterministic even when something else was up before this run.
    section "precache (network, --full only)"
    if mpv_alive || daemon_running; then
        printf 'NOTE stopping the player/daemon for the precache checks\n'
        "$CTL" stop >/dev/null 2>&1
        "$CTL" daemon-stop >/dev/null 2>&1
        for _i in $(seq 1 20); do
            mpv_alive || daemon_running || break
            sleep 0.2
        done
    fi
    check_not_ok "precache bogus id" "Invalid video ID" "$CTL" precache bogus
    check_not_ok "precache with no mpv" "Nothing playing" "$CTL" precache
    check_precache "precache $LYRICS_VID" "$CTL" precache "$LYRICS_VID"
    check_cached "precache $LYRICS_VID (cache hit)" True "$CTL" precache "$LYRICS_VID"
    # The audio cache is a cache: the downloaded file is deliberately left in
    # place for the next run (and for the daemon's next-track precache).
fi

# ----------------------------------------------------- saved queue (--full)
# queue-list and the queue edits fall back to the saved resume session only
# when no player is up, so this runs after every --full section stopped mpv
# and the daemon.
if [[ $FULL -eq 1 ]]; then
    section "saved queue (no player, --full only)"
    if mpv_alive || daemon_running; then
        printf 'NOTE stopping the player/daemon for the saved-queue checks\n'
        "$CTL" stop >/dev/null 2>&1
        "$CTL" daemon-stop >/dev/null 2>&1
        for _i in $(seq 1 20); do
            mpv_alive || daemon_running || break
            sleep 0.2
        done
    fi
    check_queue_saved
fi

# ------------------------------------------------- review regressions (offline)
# Regression checks for the 2026-10 review quick wins that need no network or
# account: the per-key background-refresh sentinel (B3) and the private-dir
# helper (B2). Importing the backend is safe because its entry point is guarded.
section "review regressions (offline)"
REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python3 - "$REPO_DIR" >"$ERR_FILE" 2>&1 <<'PY'
import importlib.util, os, sys, tempfile

repo = sys.argv[1]
path = os.path.join(repo, "backend", "yt_music.py")
spec = importlib.util.spec_from_file_location("yt_music_review_test", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

tmp = tempfile.mkdtemp(prefix="yt-music-review-")
mod.STATE_DIR = tmp

spawned = []
class _FakePopen:
    def __init__(self, *args, **kwargs):
        spawned.append(args)
mod.subprocess.Popen = _FakePopen

# Same (namespace, args) is suppressed; a different namespace is not, and the
# old shared "refresh.lock" name is gone.
assert mod.spawn_background_refresh("alpha", ["x"]) is True
assert mod.spawn_background_refresh("alpha", ["x"]) is False
assert mod.spawn_background_refresh("beta", ["x"]) is True
locks = sorted(n for n in os.listdir(tmp) if n.startswith("refresh-"))
assert len(locks) == 2, locks
assert "refresh.lock" not in locks, locks

# A different key in the same namespace gets its own sentinel.
assert mod.spawn_background_refresh("alpha", ["y"]) is True
assert len([n for n in os.listdir(tmp) if n.startswith("refresh-")]) == 3

# _ensure_private_dir tightens an already-existing loose directory to 0700.
loose = os.path.join(tmp, "loose")
os.makedirs(loose, mode=0o755)
os.chmod(loose, 0o755)
mod._ensure_private_dir(loose)
assert (os.stat(loose).st_mode & 0o777) == 0o700, oct(os.stat(loose).st_mode & 0o777)
PY
if [[ $? -eq 0 ]]; then
    pass "per-key refresh sentinel + private-dir helper"
else
    fail "per-key refresh sentinel + private-dir helper" "$(cat "$ERR_FILE")"
fi

# ------------------------------------------------- full-page playlist fetch (offline)
# The correctness sites that resolve duplicates/indices must fetch the whole
# playlist (limit=None); only the display path may keep a 100-item page.
section "full-page playlist fetch (offline)"
REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python3 - "$REPO_DIR" >"$ERR_FILE" 2>&1 <<'PY'
import importlib.util, os, sys

repo = sys.argv[1]
path = os.path.join(repo, "backend", "yt_music.py")
spec = importlib.util.spec_from_file_location("yt_music_paging_test", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

src = open(path).read()
paged = [ln for ln in src.splitlines() if "get_playlist(" in ln and "limit=100" in ln]
assert len(paged) == 1, (
    "expected exactly 1 paged get_playlist (display-only), "
    f"found {len(paged)}; total 'limit=100' occurrences={src.count('limit=100')}"
)

class FakeYT:
    def __init__(self, tracks):
        self.tracks = tracks
        self.limits = []
    def get_playlist(self, pid, limit=100, **kw):
        self.limits.append(limit)
        return {"tracks": self.tracks}

tracks = [{"videoId": f"v{i:03d}"} for i in range(150)]
fake = FakeYT(tracks)
added, duplicates, skipped, dup_ids, skip_ids = mod._add_video_ids(fake, "PL_TEST", ["v120"])
assert fake.limits == [None], fake.limits
assert duplicates == 1, duplicates
assert "v120" in dup_ids, dup_ids
PY
if [[ $? -eq 0 ]]; then
    pass "full-page playlist fetch (duplicate detection beyond 100 tracks)"
else
    fail "full-page playlist fetch (duplicate detection beyond 100 tracks)" "$(cat "$ERR_FILE")"
fi

# ------------------------------------------------- playlist edit result classification (offline)
# _confirmed_ids must strictly classify known ytmusicapi edit shapes and return
# None for anything it cannot trust; _add_video_ids then verifies against the
# playlist itself instead of assuming the whole request landed.
section "playlist edit result classification (offline)"
REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python3 - "$REPO_DIR" >"$ERR_FILE" 2>&1 <<'PY'
import importlib.util, os, sys

repo = sys.argv[1]
path = os.path.join(repo, "backend", "yt_music.py")
spec = importlib.util.spec_from_file_location("yt_music_edit_test", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Recognised shapes
assert mod._confirmed_ids(
    {"status": "STATUS_SUCCEEDED",
     "playlistEditResults": [{"videoId": "a", "setVideoId": "s1"},
                             {"videoId": "b", "setVideoId": "s2"}]},
    ["a", "b"]) == ["a", "b"]
assert mod._confirmed_ids(
    {"playlistEditResults": [
        {"playlistEditVideoAddedResultData": {"videoId": "a", "setVideoId": "s1"}}]},
    ["a"]) == ["a"]
assert mod._confirmed_ids(
    {"playlistEditResults": [{"videoId": "a", "setVideoId": "s1"},
                             {"videoId": "b"}]},
    ["a", "b"]) == ["a"]

# Unknown shapes must return None
assert mod._confirmed_ids({}, ["a"]) is None
assert mod._confirmed_ids({"playlistEditResults": "nope"}, ["a"]) is None
assert mod._confirmed_ids(
    {"playlistEditResults": [{"playlistEditVideoAddedResultData": None}]}, ["a"]) is None
assert mod._confirmed_ids(None, ["a"]) is None
assert mod._confirmed_ids([{"videoId": "a"}], ["a"]) is None

class FakeYT:
    def __init__(self, before, after, edit_response, fail_after=False):
        self.before, self.after = before, after
        self.edit_response, self.fail_after = edit_response, fail_after
        self.calls = 0
    def get_playlist(self, pid, limit=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return {"tracks": self.before}
        if self.fail_after:
            raise RuntimeError("network down")
        return {"tracks": self.after}
    def add_playlist_items(self, pid, ids, duplicates=False):
        return self.edit_response

# Unknown response: verify against the playlist itself
fake = FakeYT([{"videoId": "a"}], [{"videoId": "a"}, {"videoId": "b"}], {})
added, duplicates, skipped, dup_ids, skip_ids = mod._add_video_ids(
    fake, "PL_TEST", ["b", "c"])
assert added == 1, added
assert skipped == 1, skipped
assert fake.calls == 2, fake.calls

# Recognised response: no re-read
fake = FakeYT([], [], {"playlistEditResults": [{"videoId": "b", "setVideoId": "s"}]})
added, duplicates, skipped, dup_ids, skip_ids = mod._add_video_ids(
    fake, "PL_TEST", ["b"])
assert added == 1, added
assert fake.calls == 1, fake.calls

# Re-read failure propagates
fake = FakeYT([], [], {}, fail_after=True)
try:
    mod._add_video_ids(fake, "PL_TEST", ["b"])
except Exception:
    pass
else:
    raise AssertionError("expected _add_video_ids to raise when re-read fails")
PY
if [[ $? -eq 0 ]]; then
    pass "playlist edit result classification (recognised/unknown shapes + re-read fallback)"
else
    fail "playlist edit result classification (recognised/unknown shapes + re-read fallback)" "$(cat "$ERR_FILE")"
fi

# ------------------------------------------------- managed mpv identity gate (offline)
# monitor_mpv_events must only attach to the plugin's own mpv. A foreign
# uid-owned socket with a missing/stale pidfile must be refused, and the
# refusal logged once per distinct socket, instead of mirroring or mutating a
# foreign player.
section "managed mpv identity gate (offline)"
REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python3 - "$REPO_DIR" >"$ERR_FILE" 2>&1 <<'PY'
import importlib.util, os, sys

repo = sys.argv[1]
path = os.path.join(repo, "backend", "yt_music.py")
spec = importlib.util.spec_from_file_location("yt_music_identity_test", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

EXE = "/usr/bin/mpv"
mod.mpv_process_identity = lambda pid: ("100", EXE)
base = {"pid": 123, "start_time": "100", "executable": EXE, "socket": mod.MPV_SOCKET}

assert mod.managed_mpv_identity_matches(dict(base), EXE) is True
assert mod.managed_mpv_identity_matches({**base, "start_time": "999"}, EXE) is False
assert mod.managed_mpv_identity_matches({**base, "executable": "/usr/bin/other"}, EXE) is False
assert mod.managed_mpv_identity_matches({**base, "pid": 0}, EXE) is False
assert mod.managed_mpv_identity_matches({**base, "pid": "x"}, EXE) is False
missing = dict(base)
del missing["pid"]
assert mod.managed_mpv_identity_matches(missing, EXE) is False
assert mod.managed_mpv_identity_matches(None, EXE) is False
assert mod.managed_mpv_identity_matches("nope", EXE) is False
assert mod.managed_mpv_identity_matches({**base, "socket": "/tmp/other.sock"}, EXE) is False

mod.mpv_process_identity = lambda pid: None
assert mod.managed_mpv_identity_matches(dict(base), EXE) is False

# With no identity available monitor_mpv_events must log and return before ever
# touching the socket; socket.socket raising proves no connect is attempted.
logged = []
mod.load_mpv_pid = lambda: None
mod.get_mpv_props = lambda: {"x": 1}
mod._daemon_log = lambda message: logged.append(message)
def _must_not_connect(*args, **kwargs):
    raise AssertionError("must not connect")
mod.socket.socket = _must_not_connect
mod.monitor_mpv_events()
assert logged, "a refused socket must be logged"
PY
if [[ $? -eq 0 ]]; then
    pass "managed mpv identity gate (identity predicate + refused socket)"
else
    fail "managed mpv identity gate (identity predicate + refused socket)" "$(cat "$ERR_FILE")"
fi

# ------------------------------------------------- auth-validity marker (offline)
# The per-command network validation in get_ytmusic must be skipped while a
# recent positive check is trusted, force_auth must always revalidate, and the
# empty-playlist backstop must revalidate once. Fully offline: temp dirs plus a
# fake YTMusic replace the real config/state and network.
section "auth-validity marker (offline)"
REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python3 - "$REPO_DIR" >"$ERR_FILE" 2>&1 <<'PY'
import importlib.util, io, json, os, sys, tempfile, time
from contextlib import redirect_stdout

repo = sys.argv[1]
path = os.path.join(repo, "backend", "yt_music.py")
spec = importlib.util.spec_from_file_location("yt_music_authmark_test", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

tmp = tempfile.mkdtemp(prefix="yt-music-authmark-")
tmpcfg = os.path.join(tmp, "config")
tmpstate = os.path.join(tmp, "state")
os.makedirs(tmpcfg)
os.makedirs(tmpstate)
mod.CONFIG_DIR = tmpcfg
mod.STATE_DIR = tmpstate
mod.AUTH_VALID_PATH = os.path.join(tmpstate, "auth-valid.json")

auth_path = os.path.join(tmpcfg, "auth.json")
with open(auth_path, "w") as fh:
    json.dump({"Cookie": "SID=abc; __Secure-3PAPISID=xyz", "Origin": "https://music.youtube.com"}, fh)

count = {"n": 0}
def fake_validate(auth):
    count["n"] += 1
    return True
mod.validate_auth = fake_validate

class FakeYTMusic:
    def __init__(self, *args, **kwargs):
        pass
# Inject a stub ytmusicapi so this offline test does not require the venv.
import types
_fake_ytmusicapi = types.ModuleType("ytmusicapi")
_fake_ytmusicapi.YTMusic = FakeYTMusic
sys.modules["ytmusicapi"] = _fake_ytmusicapi

# 1. Fresh marker: no network validation.
mod._write_auth_marker()
assert mod._auth_marker_fresh() is True
before = count["n"]
mod.get_ytmusic()
assert count["n"] == before, count

# 2. Stale marker: one validation, then the marker is rewritten fresh.
with open(mod.AUTH_VALID_PATH, "w") as fh:
    json.dump({"ts": time.time() - 13 * 3600}, fh)
assert mod._auth_marker_fresh() is False
before = count["n"]
mod.get_ytmusic()
assert count["n"] == before + 1, count
assert mod._auth_marker_fresh() is True

# 3. force_auth=True revalidates even with a fresh marker.
mod._write_auth_marker()
assert mod._auth_marker_fresh() is True
before = count["n"]
mod.get_ytmusic(force_auth=True)
assert count["n"] == before + 1, count

# 4. cmd_playlists backstop: an empty library under a fresh marker revalidates
#    once and retries with a forced client.
class _FakeYtmEmpty:
    def get_library_playlists(self, limit=50):
        return []
class _FakeYtmOne:
    def get_library_playlists(self, limit=50):
        return [{"playlistId": "PL1", "title": "Mix", "count": "3"}]

calls = {"get": 0, "cleared": 0}
def fake_get_ytmusic(require_auth=True, force_auth=False):
    calls["get"] += 1
    return _FakeYtmEmpty() if calls["get"] == 1 else _FakeYtmOne()
mod.get_ytmusic = fake_get_ytmusic
mod._auth_marker_fresh = lambda: True
mod._clear_auth_marker = lambda: calls.__setitem__("cleared", calls["cleared"] + 1)

out = io.StringIO()
with redirect_stdout(out):
    mod.cmd_playlists([])
payload = json.loads(out.getvalue())
assert payload.get("ok") is True, payload
assert len(payload.get("playlists") or []) == 1, payload
assert calls["cleared"] == 1, calls
assert calls["get"] == 2, calls
PY
if [[ $? -eq 0 ]]; then
    pass "auth-validity marker (fresh skip, stale/force revalidate, empty backstop)"
else
    fail "auth-validity marker (fresh skip, stale/force revalidate, empty backstop)" "$(cat "$ERR_FILE")"
fi

# ------------------------------------------------- track-change wait (offline)
# cmd_next/cmd_prev/queue-jump must wait for the real mpv transition instead of
# sleeping a fixed second. Unit-test wait_for_track_change against a scripted
# get_mpv_props sequence. Fully offline; time.sleep is a no-op to stay fast.
section "track-change wait (offline)"
REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python3 - "$REPO_DIR" >"$ERR_FILE" 2>&1 <<'PY'
import importlib.util, os, sys

repo = sys.argv[1]
path = os.path.join(repo, "backend", "yt_music.py")
spec = importlib.util.spec_from_file_location("yt_music_trackchange_test", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

mod.time.sleep = lambda _seconds: None


def run(sequence, previous_path, previous_pos=None, timeout=5):
    calls = {"n": 0}

    def fake_props():
        i = calls["n"]
        calls["n"] += 1
        return sequence[min(i, len(sequence) - 1)]

    mod.get_mpv_props = fake_props
    props = mod.wait_for_track_change(previous_path, previous_pos, timeout)
    return props, calls["n"]


# 1. path changes on the 3rd call -> detected, and the call count is 3.
base = {"path": "/a", "playlist-pos": 0}
seq = [base, base, {"path": "/b", "playlist-pos": 1}]
props, calls = run(seq, "/a", 0)
assert props == {"path": "/b", "playlist-pos": 1}, props
assert calls == 3, calls

# 2. only playlist-pos changes (path constant) when previous_pos is given.
seq = [base, base, {"path": "/a", "playlist-pos": 1}]
props, calls = run(seq, "/a", 0)
assert props == {"path": "/a", "playlist-pos": 1}, props
assert calls == 3, calls

# 3. never changes -> returns a dict after the timeout without raising.
seq = [base]
props, calls = run(seq, "/a", 0, timeout=0.2)
assert isinstance(props, dict), props
assert props == base, props
PY
if [[ $? -eq 0 ]]; then
    pass "track-change wait (path change, pos-only change, timeout returns dict)"
else
    fail "track-change wait (path change, pos-only change, timeout returns dict)" "$(cat "$ERR_FILE")"
fi

# ------------------------------------------------- offline hardening (offline)
# parse_cookie_string keeps a quoted ';' literal, refresh_auth_headers returns
# a copy instead of mutating its argument, and the image cache predicate only
# accepts an owned regular file (never a symlink). No network, no real state
# directory touched.
section "offline hardening (offline)"
REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python3 - "$REPO_DIR" >"$ERR_FILE" 2>&1 <<'PY'
import importlib.util, os, sys, tempfile

repo = sys.argv[1]
path = os.path.join(repo, "backend", "yt_music.py")
spec = importlib.util.spec_from_file_location("yt_music_hardening_test", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# 1. One cookie parser; a ';' inside a double-quoted value is not a separator.
cookies = mod.parse_cookie_string('a=1; b="x;y"; c=3')
assert cookies == {"a": "1", "b": "x;y", "c": "3"}, cookies

# 2. refresh_auth_headers returns a fresh dict and leaves its input untouched.
source = {"Cookie": "__Secure-3PAPISID=abc; SID=def",
          "Origin": "https://music.youtube.com"}
before = dict(source)
refreshed = mod.refresh_auth_headers(source)
assert source == before, source
assert refreshed is not source, refreshed
assert refreshed.get("Authorization", "").startswith("SAPISIDHASH "), refreshed
assert "Authorization" not in source, source

# 3. The image cache predicate accepts a regular file, rejects a symlink.
tmp = tempfile.mkdtemp(prefix="yt-music-image-")
target = os.path.join(tmp, "real.jpg")
with open(target, "wb") as fh:
    fh.write(b"x")
link = os.path.join(tmp, "link.jpg")
os.symlink(target, link)
assert mod._owned_regular_file(target) is True, target
assert mod._owned_regular_file(link) is False, link
assert mod._owned_regular_file(os.path.join(tmp, "missing.jpg")) is False
PY
if [[ $? -eq 0 ]]; then
    pass "offline hardening (cookie parser, non-mutating auth, lstat image cache)"
else
    fail "offline hardening (cookie parser, non-mutating auth, lstat image cache)" "$(cat "$ERR_FILE")"
fi

printf '\nPASS %d / FAIL %d\n' "$PASS" "$FAIL"
if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
