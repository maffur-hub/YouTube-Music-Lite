#!/usr/bin/env bash
#
# Smoke test for the yt-music Omarchy plugin backend (yt-music-ctl).
#
# Usage: scripts/smoke.sh [--full]
#
#   scripts/smoke.sh          Non-destructive run. Exercises the read-only
#                             commands, the network navigation lookups and the
#                             usage/argument-error checks. Nothing on the
#                             YouTube Music account is mutated.
#   scripts/smoke.sh --full   Also exercises the playback-affecting commands:
#                             starts playback of a known videoId, pokes the
#                             transport controls and the queue, then cleans up
#                             with `stop` + `daemon-stop`.
#
# Commands ALWAYS SKIPPED (with or without --full) and why:
#   login                    interactive browser authentication
#   create-playlist          would leave a stray playlist on the account
#   playlist-add             mutates an existing playlist (incl. Liked Music)
#   remove                   deletes entries from a playlist
#   like / dislike / unlike  mutates the account's liked songs
#   daemon / watch           foreground loops that never return; the same code
#                            path is covered by ensure-daemon + daemon-stop
#
# Notes on the default (non --full) run:
#   * Playback-affecting commands (play, pause, toggle, next, prev, seek,
#     seek-pct, volume, loop, shuffle, mix, queue, queue-*, queue-add,
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

# Known-good ids used across the checks.
LYRICS_VID="HUskuj8I9xY"
NO_LYRICS_VID="dQw4w9WgXcQ"
ALBUM_ID="MPREb_OWT3CzGkUeU"
ARTIST_ID="MPLAUCCzULu3prrEaPvM2ZtkJlYQ"
PLAYLIST_ID="PLZk4b3I85c8NQaGzPvC4LuRn-KQPxg9a7"

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
MPV_SOCKET="$RUNTIME_DIR/yt-music/mpv.sock"

FULL=0
for arg in "$@"; do
    case "$arg" in
        --full) FULL=1 ;;
        -h|--help)
            sed -n '2,41p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            printf 'smoke.sh: unknown option: %s\n' "$arg" >&2
            printf 'usage: scripts/smoke.sh [--full]\n' >&2
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
# shuffle / thumbnail print nothing on success).
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

# ---------------------------------------------------------------- read-only
section "read-only"
check_ok "playlists" "$CTL" playlists
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
check_ok "playlist $PLAYLIST_ID" "$CTL" playlist "$PLAYLIST_ID"

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
if [[ -n $IDLE_MPV_PID ]] || mpv_alive; then
    check_usage "volume abc" "$CTL" volume abc
else
    skip "volume abc" "no player; cmd_volume returns 0 before validating args"
fi
check_usage "seek abc" "$CTL" seek abc

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
skip "remove" "deletes playlist entries"
skip "like / dislike / unlike" "mutates the account's liked songs"
skip "daemon / watch" "foreground loop that never returns (use ensure-daemon)"

if [[ $FULL -eq 0 ]]; then
    printf 'NOTE playback-affecting commands skipped - pass --full: play pause '
    printf 'resume toggle next prev seek seek-pct volume loop shuffle mix queue '
    printf 'queue-jump queue-remove queue-move queue-add play-next enqueue '
    printf 'enqueue-files thumbnail ensure-daemon daemon-stop\n'
fi

# ---------------------------------------------------------------- playback
if [[ $FULL -eq 1 ]]; then
    section "playback (--full)"
    check_ok "ensure-daemon" "$CTL" ensure-daemon
    check_ok "play $LYRICS_VID" "$CTL" play "$LYRICS_VID"
    check_exit0 "status" "$CTL" status
    check_ok "pause" "$CTL" pause
    check_ok "resume" "$CTL" resume
    check_exit0 "toggle" "$CTL" toggle
    check_ok "seek 5" "$CTL" seek 5
    check_ok "seek-pct 50" "$CTL" seek-pct 50
    check_ok "volume 70" "$CTL" volume 70
    check_ok "loop inf" "$CTL" loop inf
    check_ok "loop off" "$CTL" loop off
    check_exit0 "shuffle" "$CTL" shuffle
    check_ok "queue-add $NO_LYRICS_VID" "$CTL" queue-add "$NO_LYRICS_VID"
    check_ok "queue-list" "$CTL" queue-list
    check_json "queue-jump 0" "$CTL" queue-jump 0
    check_json "queue-move 1 0" "$CTL" queue-move 1 0
    check_json "queue-remove 1" "$CTL" queue-remove 1
    check_ok "play-next $NO_LYRICS_VID" "$CTL" play-next "$NO_LYRICS_VID"
    check_ok "enqueue queue album $ALBUM_ID" "$CTL" enqueue queue album "$ALBUM_ID"
    check_ok "enqueue-files queue $NO_LYRICS_VID" "$CTL" enqueue-files queue "$NO_LYRICS_VID"
    check_ok "next" "$CTL" next
    check_ok "prev" "$CTL" prev
    check_exit0 "thumbnail $LYRICS_VID" "$CTL" thumbnail "$LYRICS_VID"
    check_ok "mix $LYRICS_VID" "$CTL" mix "$LYRICS_VID"
    check_ok "queue $PLAYLIST_ID" "$CTL" queue "$PLAYLIST_ID"

    section "cleanup (--full)"
    check_ok "stop" "$CTL" stop
    check_ok "daemon-stop" "$CTL" daemon-stop
fi

printf '\nPASS %d / FAIL %d\n' "$PASS" "$FAIL"
if [[ $FAIL -gt 0 ]]; then
    exit 1
fi
exit 0
