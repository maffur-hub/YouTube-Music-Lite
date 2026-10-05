#!/usr/bin/env python3
"""yt-music-ctl — YouTube Music backend for the Omarchy bar widget.

Handles authentication, playback, playlists, likes, and search via
ytmusicapi + mpv. Status is written to ~/.local/state/yt-music/status.json.

State lives under:
  ~/.config/yt-music/       auth.json (browser cookies)
  ~/.local/state/yt-music/  status.json (read by bar widget)
  $XDG_RUNTIME_DIR/yt-music/mpv.sock  mpv IPC socket
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

STATE_DIR = os.path.expanduser("~/.local/state/yt-music")
CONFIG_DIR = os.path.expanduser("~/.config/yt-music")
STATUS_PATH = os.path.join(STATE_DIR, "status.json")
TRACK_META_PATH = os.path.join(STATE_DIR, "track-meta.json")
LAST_PLAYED_PATH = os.path.join(STATE_DIR, "last-played.json")
SESSION_PATH = os.path.join(STATE_DIR, "session.json")
# The player is respawned constantly (every mpv_play, radio, restore, resume),
# and a fresh mpv always starts at 100%. Persist the chosen volume so it is
# carried across those respawns instead of jumping back to full.
VOLUME_PATH = os.path.join(STATE_DIR, "volume.json")
LAST_PLAYED_MAX = 200
SESSION_SAVE_INTERVAL = 5
TRACK_META_MAX = 500
DAEMON_LOCK = os.path.join(STATE_DIR, "daemon.lock")
DAEMON_PID_PATH = os.path.join(STATE_DIR, "daemon.pid")
DAEMON_LOG = os.path.join(STATE_DIR, "daemon.log")
AUTH_VALID_PATH = os.path.join(STATE_DIR, "auth-valid.json")
AUTH_VALID_TTL_SECONDS = 12 * 3600
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
MPV_RUNTIME_DIR = os.path.join(RUNTIME_DIR, "yt-music")
MPV_SOCKET = os.path.join(MPV_RUNTIME_DIR, "mpv.sock")
MPV_PID_PATH = os.path.join(MPV_RUNTIME_DIR, "mpv.pid")
LIKES_TITLE = "Liked Music"
CACHE_ROOT = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "yt-music")
THUMBNAIL_CACHE_DIR = os.path.join(CACHE_ROOT, "thumbs")
IMAGE_CACHE_DIR = os.path.join(CACHE_ROOT, "images")
IMAGE_HOST_SUFFIXES = ("googleusercontent.com", "ytimg.com", "ggpht.com", "google.com")
MAX_THUMBNAIL_BYTES = 1024 * 1024
MAX_THUMBNAIL_DIMENSION = 4096
MAX_THUMBNAIL_PIXELS = 16 * 1024 * 1024
# Media caches (album art, thumbnails) are bounded; oldest files are evicted.
THUMBNAIL_CACHE_MAX_ENTRIES = 300
THUMBNAIL_CACHE_MAX_BYTES = 32 * 1024 * 1024
IMAGE_CACHE_MAX_ENTRIES = 150
IMAGE_CACHE_MAX_BYTES = 24 * 1024 * 1024

# Precached audio for queue tracks: one file per videoId under
# $XDG_CACHE_HOME/yt-music/audio (files 0600, directory 0700). The cap covers
# every file in the directory; the oldest (by mtime) are evicted first.
AUDIO_CACHE_DIR = os.path.join(CACHE_ROOT, "audio")
AUDIO_CACHE_MAX_BYTES = 512 * 1024 * 1024      # 512 MiB across the whole cache
AUDIO_CACHE_MAX_FILE_BYTES = 64 * 1024 * 1024  # 64 MiB per file (a song is far smaller)
AUDIO_PRECACHE_TIMEOUT = 60                    # seconds allowed per yt-dlp run
AUDIO_DOWNLOAD_GRACE = 300                     # age before an orphaned .dl-* dir is reaped
# The venv python has no yt_dlp module, so yt-dlp is always run as a binary.
YTDLP_BIN = "/usr/bin/yt-dlp"

# Bounded on-disk cache of API metadata responses: one JSON file per key under
# STATE_DIR/cache (files 0600, directory 0700), evicted oldest-first by stored
# ts once either cap is exceeded.
METADATA_CACHE_DIR = os.path.join(STATE_DIR, "cache")
METADATA_CACHE_MAX_ENTRIES = 400
METADATA_CACHE_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB

# Per-command TTLs, in seconds. Fast-moving screens get short TTLs (history,
# search) so refreshes stay honest; near-static reference data gets long ones
# (album/artist/lyrics), which is what makes re-opening a screen instant.
METADATA_CACHE_TTL = {
    "search": 300,     # 5 min
    "home": 600,       # 10 min
    "history": 120,    # 2 min
    "library": 900,    # 15 min
    "liked": 900,      # 15 min
    "playlist": 600,   # 10 min
    "album": 3600,     # 1 h
    "artist": 3600,    # 1 h
    "mix": 300,        # 5 min
    "radio": 300,      # 5 min
    "stations": 600,   # 10 min
    "lyrics": 86400,   # 24 h — lyrics essentially never change
}


# ---------------------------------------------------------------- helpers

def fail(msg, code=1):
    print(f"yt-music-ctl: {msg}", file=sys.stderr)
    sys.exit(code)


def _ensure_private_dir(path):
    """Create `path` with mode 0700 and tighten it if it already exists.

    os.makedirs(mode=...) does not affect directories that are already there,
    which is how the state/cache roots ended up 0755. Never raises.
    """
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def valid_video_id(value):
    return (isinstance(value, str) and len(value) == 11 and
            all(c.isalnum() or c in "_-" for c in value))


def watch_url(video_id):
    """Playback URL for a videoId.

    Always www.youtube.com rather than the music subdomain: mpv-mpris derives
    `mpris:artUrl` from a thumbnail regex that only matches youtu.be/ and
    www.youtube.com/watch URLs, so the music host would leave the Omarchy
    media widget (and any other MPRIS client) without cover art.
    """
    return f"https://www.youtube.com/watch?v={video_id}"


def _looks_like_url_title(title):
    text = str(title or "").strip().lower()
    if not text:
        return True
    if text.startswith("http://") or text.startswith("https://"):
        return True
    return ("watch?v=" in text or "youtu.be/" in text or "youtube.com/" in text)


def jpeg_dimensions(data):
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xff:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in (0xd8, 0xd9):
            continue
        if offset + 2 > len(data):
            return None
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            return None
        if marker in range(0xc0, 0xc4) or marker in range(0xc5, 0xc8) or marker in range(0xc9, 0xcc) or marker in range(0xcd, 0xd0):
            if length < 7:
                return None
            return (int.from_bytes(data[offset + 3:offset + 5], "big"),
                    int.from_bytes(data[offset + 5:offset + 7], "big"))
        offset += length
    return None


def format_duration(seconds):
    """Human-readable duration, e.g. "1 hr 27 min" / "42 min" / ""."""
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if not hours and not minutes:
        minutes = 1
    if hours and minutes:
        return f"{hours} hr {minutes} min"
    if hours:
        return f"{hours} hr"
    return f"{minutes} min"


def json_dump(path, data, mode=0o600):
    parent = os.path.dirname(path)
    # State lives in private dirs: keep them 0700 even if an older install
    # created them with the default umask.
    if (parent == STATE_DIR or parent.startswith(STATE_DIR + os.sep)
            or parent == CONFIG_DIR):
        _ensure_private_dir(parent)
    else:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def json_load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


# ---------------------------------------------------- metadata response cache

def _cache_key(namespace, args):
    """Stable file stem for a namespace plus its normalized argument list."""
    canonical = json.dumps([namespace, list(args)], sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cache_path(namespace, args):
    return os.path.join(METADATA_CACHE_DIR,
                        f"{namespace}-{_cache_key(namespace, args)}.json")


def _owned_regular_file(path):
    """True only for a regular file (never a symlink) owned by this user."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()


def cache_read(namespace, args, ttl):
    """Return (payload, fresh) for a cache key. Never raises.

    payload is the stored dict (or None when absent/corrupt); fresh is True
    only when the record is younger than ttl. Stale payloads are still
    returned so callers can fall back to them when the live fetch fails.
    """
    try:
        path = _cache_path(namespace, args)
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):  # never read through a planted link
            return None, False
        record = json_load(path, default=None)
        if not isinstance(record, dict):
            return None, False
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None, False
        try:
            fresh = (time.time() - float(record.get("ts"))) <= float(ttl)
        except (TypeError, ValueError):
            fresh = False
        return payload, bool(fresh)
    except Exception:
        return None, False


def cache_write(namespace, args, payload):
    """Store one record atomically, then evict oldest entries to stay in
    bounds (entry count and total bytes). Never raises."""
    try:
        _ensure_private_dir(METADATA_CACHE_DIR)
        record = {"ns": namespace, "ts": time.time(), "payload": payload}
        json_dump(_cache_path(namespace, args), record)
        _cache_prune()
    except Exception:
        pass


def invalidate_cache(namespace, args):
    """Unlink one cache record (regular file, owned by us). Never raises."""
    try:
        path = _cache_path(namespace, args)
        st = os.lstat(path)
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
            os.unlink(path)
    except Exception:
        pass


def invalidate_namespace(namespace):
    """Unlink every cache record written for `namespace`, whatever its args.

    Used after account mutations (like/unlike/dislike) whose cached screens
    would otherwise stay stale for the whole TTL. Cache files are prefixed
    with their namespace, so this also catches records written by older code.
    Never raises.
    """
    prefix = f"{namespace}-"
    try:
        names = os.listdir(METADATA_CACHE_DIR)
    except OSError:
        return
    for name in names:
        if not (name.startswith(prefix) and name.endswith(".json")):
            continue
        path = os.path.join(METADATA_CACHE_DIR, name)
        try:
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
                os.unlink(path)
        except Exception:
            continue


def _cache_entries():
    """(ts, size, path) for each regular cache file, oldest stored ts first.

    Falls back to mtime when the stored ts is unreadable. Symlinks and other
    non-regular files are skipped, so this never follows a planted link.
    """
    entries = []
    for name in os.listdir(METADATA_CACHE_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(METADATA_CACHE_DIR, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        record = json_load(path, default=None)
        ts = record.get("ts") if isinstance(record, dict) else None
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            ts = st.st_mtime
        entries.append((float(ts), st.st_size, path))
    entries.sort(key=lambda item: item[0])
    return entries


def _cache_prune():
    """Delete oldest entries until both caps hold. Never raises."""
    try:
        entries = _cache_entries()
        total = sum(size for _ts, size, _path in entries)
        while entries and (len(entries) > METADATA_CACHE_MAX_ENTRIES
                           or total > METADATA_CACHE_MAX_BYTES):
            _ts, size, path = entries.pop(0)
            total -= size
            try:
                st = os.lstat(path)
                if stat.S_ISREG(st.st_mode):
                    os.unlink(path)
            except OSError:
                pass
    except Exception:
        pass


def cache_clear():
    """Unlink every regular file inside the cache dir; never follows symlinks.
    Never raises."""
    try:
        for name in os.listdir(METADATA_CACHE_DIR):
            path = os.path.join(METADATA_CACHE_DIR, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
                os.unlink(path)
    except Exception:
        pass


def _strip_refresh(args):
    """Split -r/--refresh (allowed anywhere) out of a command's arguments."""
    remaining = [a for a in args if a not in ("-r", "--refresh")]
    return remaining, len(remaining) != len(args)


def _cache_served(payload, stale=False):
    """Copy a cached payload and add the marker keys printed to the caller."""
    out = dict(payload)
    out["cached"] = True
    if stale:
        out["stale"] = True
    else:
        out.pop("stale", None)
    return out


def _serve_stale(namespace, key, ttl):
    """Print the cached payload as a stale fallback. True when one existed."""
    try:
        stale, _fresh = cache_read(namespace, key, ttl)
        if stale is None:
            return False
        print(json.dumps(_cache_served(stale, stale=True)))
        return True
    except Exception:
        return False


def _ytmusic_for_cache(namespace, key, ttl, **kwargs):
    """Build a ytmusic client, printing a stale cached payload instead when
    the auth bootstrap itself fails (offline, or an expired session).

    fail() already reported the reason on stderr. Returns None only when the
    stale payload was printed and the caller must return right away; with
    nothing cached the SystemExit is re-raised so the original message and
    exit code survive unchanged.
    """
    try:
        return get_ytmusic(**kwargs)
    except SystemExit:
        if _serve_stale(namespace, key, ttl):
            return None
        raise


def spawn_background_refresh(namespace, args):
    """Refresh one stale cache entry in a detached child. Returns True when a
    refresher was spawned.

    The child re-runs the command with -r and rewrites the cache, so the next
    read is fresh. A sentinel file keeps a burst of stale reads from forking
    one refresher each: while the per-key sentinel is younger than 120 s nothing
    new is spawned, and the child deliberately never touches it (the staleness
    window makes deleting it racy). Never raises.
    """
    try:
        safe_ns = "".join(c if c.isalnum() or c in "-_" else "_"
                          for c in str(namespace))[:32] or "ns"
        seed = "\x00".join([str(namespace)] + [str(a) for a in args])
        digest = hashlib.sha256(seed.encode("utf-8", "surrogatepass")).hexdigest()[:16]
        lock = os.path.join(STATE_DIR, f"refresh-{safe_ns}-{digest}.lock")
        try:
            cutoff = time.time() - 600
            for name in os.listdir(STATE_DIR):
                if not (name.startswith("refresh-") and name.endswith(".lock")):
                    continue
                stale = os.path.join(STATE_DIR, name)
                try:
                    if os.lstat(stale).st_mtime < cutoff:
                        os.unlink(stale)
                except OSError:
                    pass
        except OSError:
            pass
        try:
            if time.time() - os.lstat(lock).st_mtime < 120:
                return False
        except OSError:
            pass
        try:
            _ensure_private_dir(STATE_DIR)
            fd = os.open(lock, os.O_CREAT | os.O_WRONLY | os.O_TRUNC
                         | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            os.utime(lock, None)  # touch, so an existing empty file ages too
        except Exception:
            pass
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "__refresh", namespace]
            + [str(a) for a in args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return True
    except Exception:
        return False


def private_runtime_dir():
    """Return whether the MPV runtime directory is a private owned directory."""
    try:
        st = os.lstat(MPV_RUNTIME_DIR)
        return (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
                and stat.S_IMODE(st.st_mode) == 0o700)
    except OSError:
        return False


def ensure_private_runtime_dir():
    os.makedirs(MPV_RUNTIME_DIR, mode=0o700, exist_ok=True)
    try:
        st = os.lstat(MPV_RUNTIME_DIR)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
                or st.st_mode & 0o077):
            raise RuntimeError("MPV runtime directory is not private")
        os.chmod(MPV_RUNTIME_DIR, 0o700)
    except OSError as exc:
        raise RuntimeError("MPV runtime directory is not private") from exc


def private_mpv_socket():
    if not private_runtime_dir():
        return False
    try:
        st = os.lstat(MPV_SOCKET)
        return stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid()
    except OSError:
        return False


def load_mpv_pid():
    """Read the managed pidfile without following an attacker-controlled link."""
    if not private_runtime_dir():
        return None
    try:
        fd = os.open(MPV_PID_PATH, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                    or st.st_mode & 0o077):
                return None
            with os.fdopen(fd) as fh:
                fd = None
                return json.load(fh)
        finally:
            if fd is not None:
                os.close(fd)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def mpv_process_identity(pid):
    """Return Linux process start time and executable for a live process."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            stat_data = fh.read()
        # The executable name is parenthesized and may itself contain spaces.
        fields = stat_data[stat_data.rfind(")") + 2:].split()
        start_time = fields[19]
        executable = os.path.realpath(os.readlink(f"/proc/{pid}/exe"))
        return start_time, executable
    except (OSError, IndexError):
        return None


def managed_mpv_identity_matches(pid_data, expected_executable=None):
    """Return whether pid_data identifies this plugin's own mpv instance.

    Rejects anything that is not a dict with a live process whose start time,
    executable and socket still match the recorded identity. When
    `expected_executable` is omitted it is recomputed from the mpv on PATH.
    """
    if not isinstance(pid_data, dict):
        return False
    pid = pid_data.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    identity = mpv_process_identity(pid)
    if identity is None:
        return False
    if expected_executable is None:
        expected_executable = os.path.realpath(shutil.which("mpv") or "")
    return (identity[0] == pid_data.get("start_time")
            and identity[1] == pid_data.get("executable")
            and identity[1] == expected_executable
            and pid_data.get("socket") == MPV_SOCKET)


def mpv_pid_record(proc):
    identity = mpv_process_identity(proc.pid)
    if not identity:
        return {"pid": proc.pid}
    return {
        "pid": proc.pid,
        "start_time": identity[0],
        "executable": identity[1],
        "socket": MPV_SOCKET,
    }


def parse_cookie_string(cookie_str):
    """Parse a Cookie header into a dict.

    Splits on `;` but treats a `;` inside a double-quoted value as literal, so
    `b="x;y"` survives as one cookie. Surrounding quotes are stripped from the
    value. Non-string input yields an empty dict.
    """
    cookies = {}
    if not isinstance(cookie_str, str):
        return cookies
    parts = []
    current = []
    in_quotes = False
    for char in cookie_str:
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
        elif char == ";" and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    for part in parts:
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        cookies[name] = value
    return cookies


def refresh_auth_headers(auth):
    """Return a copy of the auth headers with a fresh timestamped signature.

    The caller's dict is never mutated; the refreshed copy is returned so the
    caller can persist it.
    """
    if not isinstance(auth, dict):
        return auth

    refreshed = dict(auth)
    cookies = parse_cookie_string(refreshed.get("Cookie", ""))
    sapisid = cookies.get("__Secure-3PAPISID") or cookies.get("SAPISID")
    if not sapisid:
        return refreshed

    import hashlib
    origin = refreshed.get("Origin") or refreshed.get("X-Origin") or "https://music.youtube.com"
    ts = str(int(time.time()))
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    refreshed["Authorization"] = f"SAPISIDHASH {ts}_{digest}"
    return refreshed


def write_status(status):
    status["_ts"] = time.time()
    json_dump(STATUS_PATH, status)


def read_status():
    try:
        with open(STATUS_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def load_track_meta():
    """Sidecar cache of track metadata keyed by videoId (most recent last)."""
    data = json_load(TRACK_META_PATH, default={})
    return data if isinstance(data, dict) else {}


def remember_tracks(entries):
    """Merge track metadata into the sidecar cache, most-recent last."""
    if not isinstance(entries, list) or not entries:
        return
    store = load_track_meta()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("videoId")
        if not valid_video_id(video_id):
            continue
        prior = store.get(video_id)
        prior = prior if isinstance(prior, dict) else {}
        record = {}
        for key in ("title", "artist", "album", "duration"):
            value = entry.get(key)
            if not value:
                value = prior.get(key)
            if value:
                record[key] = value
        store.pop(video_id, None)
        store[video_id] = record
    while len(store) > TRACK_META_MAX:
        store.pop(next(iter(store)))
    json_dump(TRACK_META_PATH, store, mode=0o600)


def load_last_played():
    """Local play history, newest first."""
    data = json_load(LAST_PLAYED_PATH, default=[])
    return data if isinstance(data, list) else []


def remember_play(entry):
    """Prepend one played track to the local history (dedup by videoId, capped)."""
    if not isinstance(entry, dict):
        return
    video_id = entry.get("videoId")
    if not valid_video_id(video_id):
        return
    store = [e for e in load_last_played()
             if isinstance(e, dict) and e.get("videoId") != video_id]
    record = {"videoId": video_id, "playedAt": round(time.time())}
    for key in ("title", "artist", "album", "duration"):
        if entry.get(key):
            record[key] = entry[key]
    store.insert(0, record)
    del store[LAST_PLAYED_MAX:]
    json_dump(LAST_PLAYED_PATH, store, mode=0o600)


def load_session():
    data = json_load(SESSION_PATH, default={})
    return data if isinstance(data, dict) else {}


def save_session(video_ids, index, position):
    """Persist a resumable queue (videoIds + current index + time-pos)."""
    ids = [v for v in video_ids if valid_video_id(v)]
    if not ids:
        return
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = 0
    index = max(0, min(len(ids) - 1, index))
    try:
        position = max(0.0, float(position))
    except (TypeError, ValueError):
        position = 0.0
    json_dump(SESSION_PATH, {"videoIds": ids, "index": index,
                             "position": round(position, 2),
                             "updatedAt": round(time.time())}, mode=0o600)


def clear_session():
    """Drop the resumable queue (called whenever playback is intentionally replaced)."""
    try:
        unlink_private(SESSION_PATH)
    except Exception:
        pass


def load_volume(default=100):
    """The last chosen volume (0..150), or `default` when none is stored."""
    data = json_load(VOLUME_PATH, default=None)
    value = data.get("volume") if isinstance(data, dict) else data
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(150, value))


def save_volume(value):
    """Remember the volume so the next mpv respawn starts at it. Never raises."""
    try:
        value = max(0, min(150, int(value)))
    except (TypeError, ValueError):
        return
    json_dump(VOLUME_PATH, {"volume": value}, mode=0o600)


def mpv_volume_args():
    """`--volume=<n>` for a fresh mpv, carrying the persisted choice forward.

    Every spawn helper appends this to its argv; without it mpv resets to 100%
    on the next play, radio start, restore, or resume.
    """
    return [f"--volume={load_volume()}"]


def _session_ids():
    """(ids, index, position) from the saved resume session."""
    session = load_session()
    ids = session.get("videoIds") if isinstance(session, dict) else None
    ids = [v for v in ids if valid_video_id(v)] if isinstance(ids, list) else []
    try:
        index = max(0, min(len(ids) - 1, int(session.get("index") or 0))) if ids else -1
    except (TypeError, ValueError):
        index = 0 if ids else -1
    try:
        position = max(0.0, float(session.get("position") or 0))
    except (TypeError, ValueError):
        position = 0.0
    return ids, index, position


def _session_tracks(ids, index):
    meta = load_track_meta()
    tracks = []
    for i, vid in enumerate(ids):
        info = meta.get(vid) or {}
        if not isinstance(info, dict):
            info = {}
        tracks.append({
            "index": i,
            "videoId": vid,
            "title": str(info.get("title") or ""),
            "artist": str(info.get("artist") or ""),
            "album": str(info.get("album") or ""),
            "duration": info.get("duration") or 0,
            "current": i == index,
        })
    return tracks


def _session_remove(indices):
    """Remove queue indices from the saved resume session. Returns sorted removed."""
    ids, index, position = _session_ids()
    if not ids:
        return []
    drop = sorted({i for i in indices if isinstance(i, int) and 0 <= i < len(ids)})
    if not drop:
        return []
    drop_set = set(drop)
    new_ids = [v for i, v in enumerate(ids) if i not in drop_set]
    shift = sum(1 for i in drop if i < index)
    if index in drop_set:
        new_index = min(index - shift, len(new_ids) - 1) if new_ids else 0
        new_position = 0.0
    else:
        new_index = index - shift
        new_position = position
    new_index = max(0, new_index)
    if new_ids:
        save_session(new_ids, new_index, new_position)
    else:
        clear_session()
    return drop


_session_save_state = {"videoId": "", "savedAt": 0.0}


def maybe_save_session(status):
    """Snapshot the mpv queue + position for resume, throttled. Never raises."""
    try:
        video_id = str(status.get("videoId") or "")
        if not valid_video_id(video_id):
            return
        now = time.time()
        changed = video_id != _session_save_state.get("videoId")
        if not changed and now - _session_save_state.get("savedAt", 0.0) < SESSION_SAVE_INTERVAL:
            return
        playlist, pos = _playlist_state()
        if not playlist:
            return
        ids = []
        resolved_pos = -1
        for index, entry in enumerate(playlist):
            path = entry.get("filename") if isinstance(entry, dict) else ""
            path = path or ""
            vid = video_id_from_url(path) if path else ""
            if not valid_video_id(vid):
                vid = cache_audio_video_id(path) if path else ""
            if not valid_video_id(vid):
                # A stream entry cannot be resumed: skip it and keep the songs
                # rather than abandoning the whole queue.
                continue
            # Record the resolved index while walking the same order mpv
            # reported. A lookup by video id would land on the first copy of a
            # repeated track instead of the one actually playing.
            if index == pos:
                resolved_pos = len(ids)
            ids.append(vid)
        if not ids:
            return
        if not 0 <= resolved_pos < len(ids):
            # No usable positional mapping (mpv pos unreadable): fall back.
            resolved_pos = ids.index(video_id) if video_id in ids else 0
        save_session(ids, resolved_pos, status.get("position") or 0)
        _session_save_state["videoId"] = video_id
        _session_save_state["savedAt"] = now
    except Exception:
        pass


def notify_track_change(previous, current):
    """Best-effort desktop notification when the playing track changes."""
    try:
        if not current.get("playing"):
            return
        title = current.get("title") or ""
        video_id = current.get("videoId") or ""
        if not video_id or _looks_like_url_title(title):
            return
        previous = previous or {}
        if (previous.get("videoId") == video_id
                and (previous.get("title") or "") == title):
            return
        cmd = ["notify-send", "-a", "YouTube Music", "-t", "6000"]
        thumb = os.path.join(THUMBNAIL_CACHE_DIR, video_id + ".jpg")
        if os.path.exists(thumb):
            cmd.extend(["-i", thumb])
        body = current.get("artist") or ""
        album = current.get("album") or ""
        if album:
            body = f"{body} — {album}"
        cmd.extend([title, body])
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _auth_marker_fresh():
    """True while a recent successful auth check may be trusted.

    Best-effort: any missing or unreadable marker counts as not fresh so the
    caller falls back to a real validation.
    """
    try:
        data = json_load(AUTH_VALID_PATH)
        ts = float(data.get("ts") or 0)
        return time.time() - ts < AUTH_VALID_TTL_SECONDS
    except Exception:
        return False


def _write_auth_marker():
    """Record a successful auth check. Best-effort: never raises."""
    try:
        json_dump(AUTH_VALID_PATH, {"ts": time.time()})
    except Exception:
        pass


def _clear_auth_marker():
    """Drop the auth-valid marker. Best-effort: never raises."""
    try:
        os.unlink(AUTH_VALID_PATH)
    except OSError:
        pass


def get_ytmusic(require_auth=True, force_auth=False):
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        fail("ytmusicapi not installed. Run: yt-music-ctl login")
    auth_path = os.path.join(CONFIG_DIR, "auth.json")
    if not require_auth:
        return YTMusic()
    if not os.path.exists(auth_path):
        fail("Not logged in. Run: yt-music-ctl login")
    auth = json_load(auth_path)
    if not auth:
        fail("Authentication data is invalid. Run: yt-music-ctl login")
    auth = refresh_auth_headers(auth)

    # Browser cookies can expire while the local auth file still exists. In
    # that case YouTube returns an anonymous library page instead of an error,
    # which otherwise looks like an empty playlist collection. The network
    # round-trip is skipped while a recent positive check is still trusted;
    # `force_auth` (or -r/--refresh) always revalidates.
    if force_auth or not _auth_marker_fresh():
        if not validate_auth(auth):
            fresh_auth = build_browser_auth()
            if fresh_auth and validate_auth(fresh_auth):
                auth = fresh_auth
            else:
                fail("YouTube session expired. Run: yt-music-ctl login")
        _write_auth_marker()
    json_dump(auth_path, auth)
    return YTMusic(auth_path)


# ---------------------------------------------------------------- browser auth

BROWSER_COOKIE_NAMES = [
    "SID", "__Secure-1PAPISID", "__Secure-3PAPISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PSIDTS",
    "__Secure-3PSIDTS", "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "HSID", "SSID", "APISID", "LOGIN_INFO", "PREF", "SIDCC",
    "VISITOR_INFO1_LIVE", "YSC",
]

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

def build_browser_auth():
    """Build ytmusicapi auth headers directly from browser cookies."""
    import hashlib
    try:
        import browser_cookie3
    except ImportError:
        return None

    cookies = {}
    try:
        cj = browser_cookie3.chromium(domain_name=".youtube.com")
        for c in cj:
            cookies.setdefault(c.name, c.value)
    except Exception:
        try:
            cj = browser_cookie3.firefox(domain_name=".youtube.com")
            for c in cj:
                cookies.setdefault(c.name, c.value)
        except Exception:
            return None

    sapisid = cookies.get("__Secure-3PAPISID") or cookies.get("SAPISID")
    if not sapisid:
        return None

    cookie_parts = []
    for name in BROWSER_COOKIE_NAMES:
        if name in cookies:
            cookie_parts.append(f"{name}={cookies[name]}")
    cookie_str = "; ".join(cookie_parts)

    origin = "https://music.youtube.com"
    ts = str(int(time.time()))
    h = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()

    return {
        "Cookie": cookie_str,
        "Authorization": f"SAPISIDHASH {ts}_{h}",
        "Origin": origin,
        "X-Goog-AuthUser": "0",
        "X-Origin": origin,
        "X-Youtube-Bootstrap-Logged-In": "true",
        "X-Youtube-Client-Name": "67",
        "X-Youtube-Client-Version": "1.20260915.01.00",
        "User-Agent": BROWSER_UA,
    }


def validate_auth(auth):
    """Return True if the auth headers actually authenticate for library access."""
    from ytmusicapi import YTMusic
    tmp = os.path.join(CONFIG_DIR, ".auth-test.json")
    json_dump(tmp, auth)
    ok = False
    try:
        ytm = YTMusic(tmp)
        acc = ytm.get_account_info()
        ok = bool(acc and acc.get("accountName"))
    except Exception:
        ok = False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return ok


# ---------------------------------------------------------------- mpv IPC

def mpv_send(*args):
    """Send a command to mpv via IPC socket. Returns the response dict or None."""
    flat = [args[0]]
    for a in args[1:]:
        if isinstance(a, (list, tuple)):
            flat.extend(a)
        else:
            flat.append(a)
    cmd = json.dumps({"command": flat}) + "\n"
    if not private_mpv_socket():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(MPV_SOCKET)
            sock.sendall(cmd.encode())
            data = sock.recv(4096).decode()
            return json.loads(data.strip().split("\n")[0])
    except Exception:
        pass
    return None


def mpv_is_running():
    return bool(mpv_send("get_property", "mpv-version"))


def mpv_kill():
    # The resumable-queue session is deliberately NOT cleared here: Stop (and a
    # natural end of the playlist) must leave the queue resumable. The explicit
    # start-of-new-playback paths below clear it themselves.
    # Shut down a pre-runtime-dir instance on the first managed replacement.
    if private_mpv_socket():
        mpv_send("quit")
    pid_data = load_mpv_pid() or {}
    pid = pid_data.get("pid")
    identity = mpv_process_identity(pid) if isinstance(pid, int) and pid > 1 else None
    expected_executable = os.path.realpath(shutil.which("mpv") or "")
    identity_matches = managed_mpv_identity_matches(pid_data, expected_executable)
    if identity_matches:
        pidfd = None
        try:
            pidfd = os.pidfd_open(pid) if hasattr(os, "pidfd_open") else None
            # Recheck after opening the pidfd so a dead process cannot be
            # confused with a newly reused PID.
            if mpv_process_identity(pid) != identity:
                return
            if pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                signal.pidfd_send_signal(pidfd, signal.SIGTERM)
            elif mpv_process_identity(pid) == identity:
                os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 2
            while time.time() < deadline:
                try:
                    if mpv_process_identity(pid) != identity:
                        break
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.05)
            else:
                if pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                elif mpv_process_identity(pid) == identity:
                    os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        finally:
            if pidfd is not None:
                os.close(pidfd)
    paths = (MPV_SOCKET, MPV_PID_PATH) if private_runtime_dir() else ()
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def wait_for_mpv(timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mpv_send("get_property", "mpv-version"):
            return True
        time.sleep(0.1)
    return False


def wait_for_track_change(previous_path, previous_pos=None, timeout=5):
    """Poll mpv until path (or playlist-pos, when given) actually changes.

    Captured before a next/prev/jump command is sent so we can wait for the
    real transition instead of guessing with a fixed sleep. Returns the last
    props dict observed, never raises, and never blocks past timeout.
    """
    deadline = time.time() + timeout
    last = None
    while True:
        props = get_mpv_props()
        if props:
            last = props
            if props.get("path") != previous_path:
                return props
            if previous_pos is not None and props.get("playlist-pos") != previous_pos:
                return props
        if time.time() >= deadline:
            return last if last is not None else get_mpv_props()
        time.sleep(0.1)


def spawn_precache_next():
    """Fire-and-forget precache of the queue's next entry in a detached child.

    The daemon also warms the next track, but it can attach to a freshly
    started mpv during the same window a new queue is loading and miss the
    first flush. This one-shot child re-runs shortly after playback starts,
    long after the playlist is populated, so the first next-track precache is
    deterministic. It is idempotent: a cached track is detected immediately.
    Never raises.
    """
    try:
        subprocess.Popen(
            [sys.executable, os.path.realpath(__file__), "precache"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        pass


def mpv_play(video_id):
    clear_radio_current()
    ensure_daemon()
    mpv_kill()
    clear_session()
    ensure_private_runtime_dir()
    url = watch_url(video_id)
    proc = subprocess.Popen([
        "mpv",
        "--no-video",
        "--really-quiet",
        f"--input-ipc-server={MPV_SOCKET}",
        "--keep-open=no",
        "--force-seekable=yes",
        "--hr-seek=yes",
        "--ytdl",
        "--ytdl-format=bestaudio/best",
    ] + mpv_volume_args() + [url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    json_dump(MPV_PID_PATH, mpv_pid_record(proc))
    wait_for_mpv()


def mpv_control(*args):
    if not mpv_is_running():
        return None
    return mpv_send("client-message", [json.dumps(args)])


# ---------------------------------------------------------------- playback monitor

def mpv_query(names):
    """Fetch several mpv properties over one IPC connection.

    Replies are routed by request_id so an unsolicited event cannot shift the
    property mapping, and the reply stream is buffered because a single reply
    (a large playlist) can exceed one recv(). Returns {name: value} or None
    on any failure or incomplete reply.
    """
    if not names:
        return {}
    if not mpv_is_running():
        return None
    if not private_mpv_socket():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(MPV_SOCKET)
            for index, name in enumerate(names, start=1):
                cmd = json.dumps({"command": ["get_property", name],
                                  "request_id": index}) + "\n"
                sock.sendall(cmd.encode())
            pending = dict(enumerate(names, start=1))
            props = {}
            buffer = b""
            while pending:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                lines = buffer.split(b"\n")
                buffer = lines[-1]
                for line in lines[:-1]:
                    if not line.strip():
                        continue
                    try:
                        resp = json.loads(line)
                    except ValueError:
                        continue
                    index = resp.get("request_id")
                    if index in pending:
                        props[pending.pop(index)] = resp.get("data")
            if pending:
                return None
            return props
    except Exception:
        return None


def get_mpv_props():
    names = ["pause", "media-title", "metadata/by-key/artist",
             "metadata/by-key/album", "duration", "time-pos",
             "volume", "path", "filename", "loop-playlist",
             "playlist-pos", "playlist-count", "shuffle"]
    return mpv_query(names)


def wait_for_metadata(timeout=6):
    """Poll mpv until media-title is a real (non-URL) title, then give
    artist/album a brief beat to arrive so notifications get a real body."""
    deadline = time.time() + timeout
    settle_seconds = 1.0
    settle_deadline = None
    last = None
    while True:
        props = get_mpv_props()
        if props:
            last = props
            media_title = props.get("media-title", "")
            if media_title and not _looks_like_url_title(media_title):
                if settle_deadline is None:
                    settle_deadline = min(deadline, time.time() + settle_seconds)
                if (props.get("metadata/by-key/artist")
                        or props.get("metadata/by-key/album")
                        or time.time() >= settle_deadline):
                    return props
        if time.time() >= deadline:
            return last if last is not None else get_mpv_props()
        time.sleep(0.25)


def wait_for_stream_progress(stream_url, timeout=8):
    """True once a live stream is actually playing, not just connected.

    A dead station URL can still be accepted by mpv and keep the process alive
    while producing no audio, so `mpv_is_running()` alone reports a false
    success. A live stream is real when either its position advances across two
    polls or mpv reports a title that is genuinely ICY metadata rather than the
    stream's own URL/filename fragment (mpv echoes the last path segment as the
    title before any metadata arrives, which `_looks_like_url_title` alone does
    not catch). Returns (ok, last_props).
    """
    deadline = time.time() + timeout
    last = None
    first_pos = None
    while time.time() < deadline:
        props = get_mpv_props()
        if props:
            last = props
            title = props.get("media-title", "")
            if _radio_now_playing(title, stream_url):
                # A real ICY title (never the stream URL fragment) means audio.
                return True, props
            pos = props.get("time-pos")
            if isinstance(pos, (int, float)):
                if first_pos is None:
                    first_pos = pos
                elif pos > first_pos + 0.25:
                    return True, props
        time.sleep(0.25)
    return False, last if last is not None else get_mpv_props()


def video_id_from_url(url):
    """Pull the video id out of a YouTube watch/youtu.be URL.

    Only YouTube hosts are parsed. A non-YouTube stream URL that happens to
    carry a `v=` (or `device=`) query parameter must not masquerade as a
    video id. A precached queue entry plays as a local file rather than a
    URL, so it still resolves by its file name first.
    """
    if not isinstance(url, str) or not url:
        return None
    # A precached queue entry plays as a local file rather than a URL.
    local = cache_audio_video_id(url)
    if local:
        return local
    parsed = urllib.parse.urlsplit(url)
    # A trailing dot is a valid FQDN root ("youtube.com.") but would defeat
    # the suffix checks below, so strip it before matching the host.
    host = (parsed.hostname or "").lower().rstrip(".")
    if host == "youtu.be":
        segment = parsed.path.lstrip("/").split("/")[0].split("?")[0]
        return segment or None
    if (host in ("youtube.com", "www.youtube.com", "m.youtube.com",
                 "music.youtube.com", "youtube-nocookie.com")
            or host.endswith(".youtube.com")
            or host.endswith(".youtube-nocookie.com")):
        return urllib.parse.parse_qs(parsed.query).get("v", [None])[0]
    return None


def _is_youtube_url(url):
    """True when a URL points at a YouTube host (watch, music, or short link).

    video_id_from_url already parses exactly those hosts, but an empty `v=`
    makes it return None just like a non-YouTube host, so stream detection
    needs this to keep a YouTube watch URL from masquerading as a live stream.
    """
    # Match video_id_from_url's host set, trailing dot included, so a
    # no-cookie or FQDN-root YouTube URL is never read as a live stream.
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    return (host == "youtu.be" or host == "youtube.com"
            or host.endswith(".youtube.com")
            or host == "youtube-nocookie.com"
            or host.endswith(".youtube-nocookie.com"))


def extract_video_id(props):
    if not props:
        return None
    path = props.get("path", "") or props.get("filename", "")
    return video_id_from_url(path)


def write_status_from_mpv(props, notify=True, spawn_precache=False):
    if not props:
        status = {"ok": True, "playing": False}
        write_status(status)
        return status
    radio = load_radio_current()
    paused = props.get("pause", True)
    title = props.get("media-title", "")
    if _looks_like_url_title(title):
        title = ""
    artist = props.get("metadata/by-key/artist", "")
    album = props.get("metadata/by-key/album", "")
    # A precached local file carries no stream metadata: mpv reports its file
    # name as the title, so take title/artist/album from the track sidecar.
    local_id = cache_audio_video_id(props.get("path") or props.get("filename") or "")
    if local_id:
        local_meta = load_track_meta().get(local_id)
        local_meta = local_meta if isinstance(local_meta, dict) else {}
        title = str(local_meta.get("title") or "")
        artist = str(local_meta.get("artist") or "")
        album = str(local_meta.get("album") or "")
    duration = props.get("duration", 0) or 0
    position = props.get("time-pos", 0) or 0
    volume = props.get("volume", 100)
    # A volume change made outside `volume` (media keys/MPRIS) is still the
    # user's choice: persist it so the next respawn carries it forward.
    try:
        volume_int = max(0, min(150, int(round(float(volume)))))
        if volume_int != load_volume():
            save_volume(volume_int)
    except (TypeError, ValueError):
        pass
    video_id = extract_video_id(props)
    # The marker reads "a stream is queued", not "a stream is playing": mpv can
    # have YouTube tracks appended after the stream, and only the entry with no
    # video id (the stream URL) is live. A per-track mismatch leaves the marker
    # in place so jumping back to the stream entry goes live again.
    live = bool(radio) and not video_id
    previous = read_status()
    status = {
        "ok": True,
        "playing": not paused,
        "paused": bool(paused),
        "title": str(title or ""),
        "artist": str(artist or ""),
        "album": str(album or ""),
        "videoId": video_id or "",
        "duration": round(float(duration)),
        "position": round(float(position)),
        "volume": round(float(volume)),
        "loop": str(props.get("loop-playlist") or "no"),
        "shuffle": bool(props.get("shuffle")),
        "playlistPos": props.get("playlist-pos"),
        "playlistCount": props.get("playlist-count"),
    }
    if live:
        # A live station plays through the shared mpv pipeline, but must
        # stay out of history, session, sidecar and precache: stamp the
        # station on the status and let the ICY track ride along.
        now_playing = _radio_now_playing(props.get("media-title", ""),
                                         radio.get("url", ""))
        station_name = str(radio.get("name", ""))
        status["live"] = True
        status["source"] = "radio"
        status["stationId"] = radio.get("id", "")
        status["stationName"] = station_name
        status["stationFavicon"] = str(radio.get("favicon") or "")
        status["nowPlaying"] = now_playing
        status["title"] = station_name
        status["artist"] = now_playing
        status["album"] = ""
    if (not live and status["playing"] and video_id
            and video_id != str(previous.get("videoId") or "")):
        sidecar = load_track_meta().get(video_id) or {}
        remember_play({
            "videoId": video_id,
            "title": status.get("title") or sidecar.get("title") or "",
            "artist": status.get("artist") or sidecar.get("artist") or "",
            "album": status.get("album") or sidecar.get("album") or "",
            "duration": status.get("duration") or sidecar.get("duration") or 0,
        })
    if not live:
        maybe_save_session(status)
    if notify and not live:
        notify_track_change(previous, status)
    write_status(status)
    # A one-shot CLI playback start schedules the next-track precache in a
    # detached child, so the first warm does not depend on when the daemon
    # happens to attach to the new mpv. The daemon never sets this flag.
    if spawn_precache and status.get("playing") and video_id and not live:
        spawn_precache_next()
    return status


# ------------------------------------------------- internet radio (Radio Browser)

# Directory lookups go to the community Radio Browser API. Its servers are
# volunteer-run mirrors behind a round-robin name, so a request tries the
# stable entry point first and then each mirror directly. The directory asks
# clients to identify themselves with a descriptive User-Agent.
RADIO_BROWSER_UA = ("yt-music-bar/2.2.0 "
                    "(+https://github.com/maffur-hub/youtube-music-bar)")
RADIO_BROWSER_SERVERS = [
    "all.api.radio-browser.info",
    "de1.api.radio-browser.info",
    "de2.api.radio-browser.info",
    "nl1.api.radio-browser.info",
    "fi1.api.radio-browser.info",
]
RADIO_BROWSER_TIMEOUT = 12  # seconds per mirror
MAX_RADIO_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB cap per response

# Hand-picked stations that ship with the plugin. Embedded rather than kept as
# a separate JSON file because install.sh copies backend/yt_music.py into
# ~/.local/share/yt-music/ as a lone file, so a sibling data file would never
# be installed. The raw field names below are adapted at read time by
# normalize_station, the same normalizer used for live directory rows.
RADIO_CATALOG = [
    {"uuid": "06faec0e-52eb-11e8-a4d1-52543be04c81", "name": "Triple J", "url": "http://abc.streamguys1.com/live/triplejnsw/icecast.audio", "favicon": "http://www.abc.net.au/core-assets/triplej/favicon-32x32.png", "homepage": "http://www.abc.net.au/triplej/", "tags": "", "country": "AU", "codec": "AAC+", "bitrate": 56, "votes": 916},
    {"uuid": "5547b37d-c1fb-40a5-af58-2f504bc0a6c9", "name": "Double J QLD", "url": "https://mediaserviceslive.akamaized.net/hls/live/2038342/doublejqld/index.m3u8", "favicon": "", "homepage": "https://www.abc.net.au/listen/doublej", "tags": "", "country": "AU", "codec": "AAC", "bitrate": 252, "votes": 30},
    {"uuid": "1c6dcd6f-88c6-4fd4-8191-078435168e85", "name": "BBC Radio 6 Music", "url": "http://as-hls-ww-live.akamaized.net/pool_81827798/live/ww/bbc_6music/bbc_6music.isml/bbc_6music-audio%3d320000.norewind.m3u8", "favicon": "https://de8as167a043l.cloudfront.net/styles/images/logosplus/120x120xBBCR6.png", "homepage": "https://www.bbc.co.uk/sounds/play/live:bbc_6music", "tags": "alternative,blues,dance,eclectic,electronic,experimental,funk,grime,hip hop,house,indie,jazz,metal,pop,punk,r&b,reggae,rock,ska,soul,techno,world", "country": "GB", "codec": "UNKNOWN", "bitrate": 0, "votes": 3326},
    {"uuid": "98adecf7-2683-4408-9be7-02d3f9098eb8", "name": "BBC World Service", "url": "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service", "favicon": "http://cdn-profiles.tunein.com/s24948/images/logoq.jpg?t=1", "homepage": "https://www.bbc.co.uk/programmes/w172xzjgf6lxp7y", "tags": "news,talk", "country": "GB", "codec": "MP3", "bitrate": 56, "votes": 164407},
    {"uuid": "445cbb3a-1c4e-49aa-a268-f5b6acfa8f2e", "name": "KEXP 90.3 Seattle, WA (AAC 160K)", "url": "https://kexp.streamguys1.com/kexp160.aac", "favicon": "http://www.kexp.org/static/assets/img/favicon-32x32.png", "homepage": "https://www.kexp.org/", "tags": "", "country": "US", "codec": "AAC", "bitrate": 162, "votes": 634},
    {"uuid": "6238f5e8-a9ee-4c88-9713-2d1ab4112ac9", "name": "KCRW Eclectic 24 (AAC)", "url": "https://streams.kcrw.com/e24_aac", "favicon": "https://www.kcrw.com/++theme++kcrw.theme/icons/apple-touch-icon.png", "homepage": "https://www.kcrw.com/music/shows/eclectic24", "tags": "music", "country": "US", "codec": "AAC", "bitrate": 256, "votes": 82},
    {"uuid": "932eb148-e6f6-11e9-a96c-52543be04c81", "name": "FIP", "url": "http://icecast.radiofrance.fr/fip-hifi.aac", "favicon": "https://upload.wikimedia.org/wikipedia/fr/thumb/d/d5/FIP_logo_2005.svg/1024px-FIP_logo_2005.svg.png", "homepage": "https://www.fip.fr/", "tags": "aac,music,public radio,radio france", "country": "FR", "codec": "AAC", "bitrate": 192, "votes": 43531},
    {"uuid": "9617a958-0601-11e8-ae97-52543be04c81", "name": "Radio Paradise Main Mix (EU) 320k AAC", "url": "http://stream-uk1.radioparadise.com/aac-320", "favicon": "https://radioparadise.com/apple-touch-icon.png", "homepage": "https://radioparadise.com/", "tags": "california,eclectic,free,internet,non-commercial,paradise,radio", "country": "US", "codec": "AAC", "bitrate": 320, "votes": 318329},
    {"uuid": "e6fa9a8a-02a8-11e9-a1be-52543be04c81", "name": "SomaFM Groove Salad Classic (128k MP3)", "url": "https://ice2.somafm.com/gsclassic-128-mp3", "favicon": "https://somafm.com/img3/gsclassic400.jpg", "homepage": "https://somafm.com/groovesalad/", "tags": "ambient,downtempo,early 2000s", "country": "US", "codec": "MP3", "bitrate": 160, "votes": 1362},
    {"uuid": "9067dc39-4bf4-4364-bd6b-8020cff3e15d", "name": "Nightride FM - Chillsynth", "url": "https://stream.nightride.fm/chillsynth.mp3", "favicon": "https://nightride.fm/thumbnail.png", "homepage": "https://nightride.fm/", "tags": "chillsynth,chillwave,instrumental", "country": "DE", "codec": "MP3", "bitrate": 320, "votes": 1665},
    {"uuid": "1d730b8a-49e2-403d-870c-07b1e27be7fd", "name": "Jazz24", "url": "https://knkx-live-a.edge.audiocdn.com/6285_256k", "favicon": "", "homepage": "https://www.jazz24.org/", "tags": "", "country": "US", "codec": "AAC", "bitrate": 256, "votes": 302},
    {"uuid": "3487079b-91b1-4fb8-b315-c4150e705b7a", "name": "WBGO Jazz 88.3 FM", "url": "https://ais-sa8.cdnstream1.com/3629_128.mp3", "favicon": "", "homepage": "https://www.wbgo.org/", "tags": "jazz", "country": "US", "codec": "MP3", "bitrate": 128, "votes": 828},
    {"uuid": "96077079-0601-11e8-ae97-52543be04c81", "name": "Radio Swiss Classic German", "url": "http://stream.srg-ssr.ch/m/rsc_de/mp3_128", "favicon": "", "homepage": "http://www.radioswissclassic.ch/", "tags": "classical,public radio,srg ssr", "country": "CH", "codec": "MP3", "bitrate": 128, "votes": 9373},
    {"uuid": "961ac56b-0601-11e8-ae97-52543be04c81", "name": "Radio Swiss Jazz", "url": "http://stream.srg-ssr.ch/m/rsj/mp3_128", "favicon": "", "homepage": "http://www.radioswissjazz.ch/", "tags": "jazz,public radio,srg ssr", "country": "CH", "codec": "MP3", "bitrate": 128, "votes": 27317},
    {"uuid": "9c2115ee-1bfe-4ca6-981e-2104d7636aee", "name": "Frisky Radio", "url": "https://stream.frisky.friskyradio.com/mp3_low", "favicon": "https://s3.amazonaws.com/media.friskyradio.com/favicon.png", "homepage": "https://www.friskyradio.com/", "tags": "", "country": "US", "codec": "MP3", "bitrate": 96, "votes": 1085},
    {"uuid": "961e6cac-0601-11e8-ae97-52543be04c81", "name": "NTS Radio 1", "url": "http://stream-relay-geo.ntslive.net/stream", "favicon": "http://www.nts.live/favicon.ico", "homepage": "http://www.nts.live/", "tags": "community radio,dj sets,eclectic,freeform", "country": "GB", "codec": "MP3", "bitrate": 256, "votes": 1845},
    {"uuid": "b8634a5a-6a46-432c-bc01-f1e3a3c4b2bf", "name": "Rinse FM", "url": "https://admin.stream.rinse.fm/proxy/rinse_uk/stream", "favicon": "", "homepage": "https://www.rinse.fm/", "tags": "", "country": "GB", "codec": "AAC+", "bitrate": 128, "votes": 20},
]


def _int_or_zero(value):
    """Coerce a directory field to int, defaulting to 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _radio_browser_request(path, params):
    """GET one Radio Browser endpoint, trying each mirror in turn.

    Returns the parsed JSON body from the first mirror that answers. Raises the
    last error once every mirror has failed; the command layer decides whether
    to fall back to a stale cache or report the failure.
    """
    query = urllib.parse.urlencode(params or {})
    last_error = None
    for server in RADIO_BROWSER_SERVERS:
        url = f"https://{server}{path}"
        if query:
            url = f"{url}?{query}"
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": RADIO_BROWSER_UA})
            with urllib.request.urlopen(
                    request, timeout=RADIO_BROWSER_TIMEOUT) as response:
                body = response.read(MAX_RADIO_RESPONSE_BYTES)
            return json.loads(body.decode("utf-8"))
        except Exception as e:  # try the next mirror
            last_error = e
    if last_error is not None:
        raise last_error
    raise RuntimeError("no Radio Browser servers configured")


def _radio_now_playing(media_title, stream_url):
    """ICY title for a stream, or "" when mpv only reports a URL fragment.

    mpv reports the URL's last path segment (e.g. "icecast.audio") as
    media-title until the stream supplies ICY metadata, so such a value must
    not be shown as if it were a song title.
    """
    t = str(media_title or "").strip()
    if not t or _looks_like_url_title(t):
        return ""
    raw = str(stream_url or "")
    parsed = urllib.parse.urlsplit(raw)
    tail = raw.rsplit("/", 1)[-1]
    candidates = {raw, parsed.hostname or "",
                  parsed.path.rstrip("/").rsplit("/", 1)[-1],
                  tail, tail.split("?")[0]}
    candidates = {c.lower() for c in candidates if c}
    return "" if t.lower() in candidates else t


def _radio_browser_report_click(station_id):
    """Best-effort click report so the directory's stats count this play.

    Silent by design: a failed report must never affect playback, and user
    stations have no directory id to report.
    """
    try:
        station_id = str(station_id or "")
        if not station_id or station_id.startswith("user:"):
            return
        path = "/json/url/" + urllib.parse.quote(station_id)
        for server in RADIO_BROWSER_SERVERS:
            try:
                request = urllib.request.Request(
                    f"https://{server}{path}",
                    headers={"User-Agent": RADIO_BROWSER_UA})
                with urllib.request.urlopen(
                        request, timeout=RADIO_BROWSER_TIMEOUT):
                    return
            except Exception:
                continue
    except Exception:
        pass


def normalize_station(raw, source):
    """Map one directory or catalog row to the widget's station shape.

    Accepts both live Radio Browser keys (stationuuid, url_resolved,
    countrycode) and the embedded catalog keys (uuid, url, country), so a
    single normalizer serves both. Returns None for a row without a playable
    http(s) stream URL.
    """
    if not isinstance(raw, dict):
        return None
    url = raw.get("url_resolved") or raw.get("url") or ""
    scheme = urllib.parse.urlsplit(str(url)).scheme.lower()
    if scheme not in ("http", "https") or not str(url).strip():
        return None
    raw_tags = raw.get("tags")
    if isinstance(raw_tags, list):
        tags = [str(t).strip() for t in raw_tags]
    else:
        tags = [t.strip() for t in str(raw_tags or "").split(",")]
    tags = [t for t in tags if t]
    return {
        "kind": "station",
        "id": str(raw.get("stationuuid") or raw.get("uuid")
                  or raw.get("id") or ""),
        "name": str(raw.get("name") or ""),
        "url": str(url),
        "favicon": str(raw.get("favicon") or ""),
        "homepage": str(raw.get("homepage") or ""),
        "tags": tags,
        "country": str(raw.get("countrycode") or raw.get("country") or ""),
        "codec": str(raw.get("codec") or ""),
        "bitrate": _int_or_zero(raw.get("bitrate")),
        "votes": _int_or_zero(raw.get("votes")),
        "source": source,
    }


# -------------------------------------------------------- radio favorites

# Saved stations live in one private JSON file. The read path refuses links
# and FIFOs and refuses oversized files, so a planted file cannot turn into an
# unbounded read; the write path clamps every string and tag length so a
# hostile directory row cannot inflate the file without bound.
RADIO_STATIONS_PATH = os.path.join(STATE_DIR, "radio-stations.json")
# The editable Featured list stores only deltas over RADIO_CATALOG: the rows
# the user added and the catalog ids the user hid. Keeping deltas means a
# future catalog update still flows through without clobbering user edits.
RADIO_FEATURED_PATH = os.path.join(STATE_DIR, "radio-featured.json")
RADIO_STATIONS_MAX = 500
RADIO_FIELD_MAX = 512


def _read_private_json(path, cap_bytes=1024 * 1024, default=None):
    """Read a small user-owned JSON file, never following a symlink or FIFO.

    Opens O_NOFOLLOW|O_NONBLOCK and validates the already-open fd, so a link
    swapped in after the check cannot redirect the read. The size cap is
    enforced before a byte is loaded. Any failure yields `default`.
    """
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return default
        if st.st_uid != os.getuid():
            return default
        if not 0 <= st.st_size <= cap_bytes:
            return default
        chunks = []
        remaining = cap_bytes
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return json.loads(b"".join(chunks).decode("utf-8"))
    except Exception:
        return default
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _ensure_station_id(row):
    """Give a normalized station a stable id when its source had none.

    Without this the id is "" and every id-less entry collapses under the same
    key in the by-id maps used by load/save, silently dropping all but one.
    """
    if not row.get("id"):
        digest = hashlib.sha1(
            str(row.get("url") or "").encode("utf-8")).hexdigest()
        row["id"] = "user:" + digest
    return row


def load_radio_stations():
    """Saved radio stations, deduped by id, capped, and normalized.

    Tolerates either the versioned {"stations": [...]} wrapper or a bare list,
    so a partially written or hand-edited file still loads. Never raises.
    """
    try:
        data = _read_private_json(RADIO_STATIONS_PATH, default={})
        if isinstance(data, dict):
            entries = data.get("stations")
        else:
            entries = data
        if not isinstance(entries, list):
            return []
        by_id = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            row = normalize_station(entry, entry.get("source") or "favorite")
            if row:
                _ensure_station_id(row)
                by_id[row["id"]] = row
        return list(by_id.values())[:RADIO_STATIONS_MAX]
    except Exception:
        return []


def save_radio_stations(items):
    """Normalize, dedupe, clamp and write saved stations atomically.

    Field and tag lengths are clamped to keep the on-disk file bounded. Never
    raises.
    """
    try:
        by_id = {}
        for entry in items or []:
            if not isinstance(entry, dict):
                continue
            row = normalize_station(entry, entry.get("source") or "favorite")
            if not row:
                continue
            _ensure_station_id(row)
            for key in ("id", "name", "url", "favicon", "homepage", "country",
                        "codec", "source"):
                row[key] = str(row.get(key) or "")[:RADIO_FIELD_MAX]
            row["tags"] = [str(t)[:64] for t in row.get("tags") or []][:32]
            by_id[row["id"]] = row
        cleaned = list(by_id.values())[:RADIO_STATIONS_MAX]
        json_dump(RADIO_STATIONS_PATH, {"version": 1, "stations": cleaned},
                  mode=0o600)
    except Exception:
        pass


def _catalog_featured_ids():
    """Ids of every embedded RADIO_CATALOG row that normalizes cleanly."""
    ids = set()
    for raw in RADIO_CATALOG:
        row = normalize_station(raw, "catalog")
        if row:
            _ensure_station_id(row)
            ids.add(row["id"])
    return ids


def load_featured():
    """The user's Featured deltas: added rows plus hidden catalog ids.

    Tolerates either the versioned {"added": [...], "hidden": [...]} wrapper or
    a bare list of added rows, so a hand-edited file still loads. Added rows
    are normalized, id-assigned, deduped and capped; hidden is a deduped list
    of non-empty strings. Never raises.
    """
    try:
        data = _read_private_json(RADIO_FEATURED_PATH, default={})
        if isinstance(data, dict):
            entries = data.get("added")
            hidden = data.get("hidden")
        else:
            entries = data
            hidden = []
        added = []
        seen = set()
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                row = normalize_station(entry,
                                        entry.get("source") or "favorite")
                if not row:
                    continue
                _ensure_station_id(row)
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                added.append(row)
                if len(added) >= RADIO_STATIONS_MAX:
                    break
        if isinstance(hidden, list):
            hidden = [str(h).strip() for h in hidden]
            hidden = [h for h in hidden if h]
            hidden = list(dict.fromkeys(hidden))
        else:
            hidden = []
        return {"added": added, "hidden": hidden}
    except Exception:
        return {"added": [], "hidden": []}


def save_featured(added, hidden):
    """Write the Featured deltas privately, in the versioned wrapper.

    Kept separate from save_radio_stations so favorites and Featured stay
    independent files. Never raises.
    """
    try:
        json_dump(RADIO_FEATURED_PATH,
                  {"version": 1, "added": added, "hidden": hidden},
                  mode=0o600)
    except Exception:
        pass


def _featured_items():
    """The merged Featured list: user additions first, then catalog rows.

    Catalog ids the user hid are dropped, and a catalog id already present in
    the additions is not duplicated, so a user's edit of a catalog station
    wins without dropping the catalog row entirely. Shared by the catalog
    command and the add/remove counts so the three cannot drift.
    """
    state = load_featured()
    hidden = set(state["hidden"])
    items = []
    seen = set()
    for row in state["added"]:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        items.append(row)
    for raw in RADIO_CATALOG:
        row = normalize_station(raw, "catalog")
        if not row:
            continue
        _ensure_station_id(row)
        if row["id"] in hidden or row["id"] in seen:
            continue
        seen.add(row["id"])
        items.append(row)
    return items


# --------------------------------------------------- radio marker + history

# The live marker is the one thing that tells write_status_from_mpv a stream
# is playing, so the shared status pipeline (bar pill, Hero) can show it while
# it stays out of the YouTube history/session/sidecar/precache. The play
# history is a small, separate, capped list of the stations themselves.
RADIO_CURRENT_PATH = os.path.join(STATE_DIR, "radio-current.json")
RADIO_HISTORY_PATH = os.path.join(STATE_DIR, "radio-history.json")
RADIO_HISTORY_MAX = 50


def load_radio_current():
    """The live radio marker, or None when no stream is marked playing."""
    data = _read_private_json(RADIO_CURRENT_PATH, default={})
    if (isinstance(data, dict) and data.get("live") is True
            and data.get("url")):
        return data
    return None


def set_radio_current(record):
    """Stamp the live marker so status writes describe the station."""
    json_dump(RADIO_CURRENT_PATH, {**record, "live": True}, mode=0o600)


def clear_radio_current():
    """Drop the live radio marker (regular file owned by us). Never raises."""
    try:
        st = os.lstat(RADIO_CURRENT_PATH)
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
            os.unlink(RADIO_CURRENT_PATH)
    except Exception:
        pass


def record_radio_play(station):
    """Prepend a station to the radio history (dedup by id, capped)."""
    try:
        if not isinstance(station, dict):
            return
        station_id = str(station.get("id") or "")
        if not station_id:
            return
        store = [e for e in load_radio_history()
                 if isinstance(e, dict) and e.get("id") != station_id]
        tags = station.get("tags")
        record = {
            "id": station_id[:RADIO_FIELD_MAX],
            "name": str(station.get("name") or "")[:RADIO_FIELD_MAX],
            "url": str(station.get("url") or "")[:RADIO_FIELD_MAX],
            "favicon": str(station.get("favicon") or "")[:RADIO_FIELD_MAX],
            "tags": ([str(t)[:64] for t in tags][:32]
                     if isinstance(tags, list) else []),
            "country": str(station.get("country") or "")[:RADIO_FIELD_MAX],
            "codec": str(station.get("codec") or "")[:RADIO_FIELD_MAX],
            "bitrate": _int_or_zero(station.get("bitrate")),
            "playedAt": round(time.time()),
        }
        store.insert(0, record)
        del store[RADIO_HISTORY_MAX:]
        json_dump(RADIO_HISTORY_PATH, store, mode=0o600)
    except Exception:
        pass


def load_radio_history():
    """Recently played radio stations, newest first. Never raises."""
    try:
        data = _read_private_json(RADIO_HISTORY_PATH, default=[])
        if isinstance(data, dict):
            entries = data.get("stations")
        else:
            entries = data
        if not isinstance(entries, list):
            return []
        items = []
        for entry in entries[:RADIO_HISTORY_MAX]:
            if not isinstance(entry, dict):
                continue
            tags = entry.get("tags")
            items.append({
                "id": str(entry.get("id") or ""),
                "name": str(entry.get("name") or ""),
                "url": str(entry.get("url") or ""),
                "favicon": str(entry.get("favicon") or ""),
                "tags": tags if isinstance(tags, list) else [],
                "country": str(entry.get("country") or ""),
                "codec": str(entry.get("codec") or ""),
                "bitrate": _int_or_zero(entry.get("bitrate")),
                "playedAt": _int_or_zero(entry.get("playedAt")),
            })
        return items
    except Exception:
        return []


# ---------------------------------------------------------------- commands

def cmd_login(args):
    auth_path = os.path.join(CONFIG_DIR, "auth.json")
    _ensure_private_dir(CONFIG_DIR)
    _clear_auth_marker()

    # try to read auth straight from the browser cookies — no manual paste
    if "--manual" not in args:
        print("Reading YouTube cookies from your browser...")
        auth = build_browser_auth()
        if auth:
            if validate_auth(auth):
                json_dump(auth_path, auth)
                _write_auth_marker()
                print(f"Success! Auth saved to {auth_path}")
                print("You can now use yt-music-ctl: playlists, search, play, mix, like")
                return
            print("  Cookies found, but they did not authenticate.")
        else:
            print("  No YouTube session found in your browser.")

    # fall back to the manual header-paste flow
    try:
        from ytmusicapi import setup
    except ImportError:
        fail("ytmusicapi not installed")
    print("Opening music.youtube.com in your browser...")
    try:
        subprocess.Popen(["xdg-open", "https://music.youtube.com"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    print()
    print("Step 1: Log in to YouTube Music in the browser.")
    print("Step 2: Open Developer Tools (F12) → Network tab → reload the page.")
    print("Step 3: Click any request, right-click → Copy → Copy request headers.")
    print("Step 4: Paste the headers below, then press Ctrl-D when done.")
    print()
    try:
        setup(filepath=auth_path)
    except (EOFError, KeyboardInterrupt):
        fail("Login cancelled — no changes made.")
    except Exception as e:
        fail(f"Login failed: {e}")
    try:
        os.chmod(auth_path, 0o600)
    except OSError:
        pass
    print(f"Auth saved to {auth_path}")
    print("You can now use yt-music-ctl commands: playlists, search, play, mix")


def cmd_logout(args):
    """Remove the locally stored YouTube Music authentication.

    The auth file is browser-derived; deleting it only logs this plugin out.
    It never touches anything in the browser or the YouTube account.
    """
    auth_path = os.path.join(CONFIG_DIR, "auth.json")
    removed = False
    try:
        st = os.lstat(auth_path)
        if stat.S_ISREG(st.st_mode):
            os.unlink(auth_path)
            removed = True
    except FileNotFoundError:
        pass
    except OSError as e:
        fail(f"Could not remove {auth_path}: {e}")
    _clear_auth_marker()
    print(json.dumps({"ok": True, "removed": removed, "authPath": auth_path}))


def cmd_status(args):
    if not mpv_is_running():
        # No player means no stream can be live: reconcile a marker left behind
        # by an unexpected mpv death so a status reader cannot see it as live.
        clear_radio_current()
        write_status({"ok": True, "playing": False})
        return
    props = get_mpv_props()
    if not props:
        clear_radio_current()
        write_status({"ok": True, "playing": False})
        return
    write_status_from_mpv(props)


def track_from_args(video_id, extra):
    """Build a sidecar entry from optional CLI args: [title artist duration]."""
    entry = {"videoId": video_id}
    if len(extra) > 0 and extra[0]:
        entry["title"] = extra[0]
    if len(extra) > 1 and extra[1]:
        entry["artist"] = extra[1]
    if len(extra) > 2:
        try:
            seconds = int(float(extra[2]))
        except ValueError:
            seconds = 0
        if seconds > 0:
            entry["duration"] = seconds
    return entry


def cmd_play(args):
    if not args:
        fail("Usage: yt-music-ctl play <videoId>")
    video_id = args[0]
    if not valid_video_id(video_id):
        fail("Invalid video ID")
    mpv_play(video_id)
    props = wait_for_metadata()
    if not mpv_is_running():
        # A well-formed id can still be unplayable (removed/region-locked), and
        # mpv then exits before producing metadata. Report that instead of a
        # false success and a status file that briefly claims it is playing.
        write_status({"ok": False, "playing": False})
        print(json.dumps({"ok": False, "error": "Playback failed"}))
        return
    write_status_from_mpv(props, spawn_precache=True)
    title = (props or {}).get("media-title") or ""
    if _looks_like_url_title(title):
        title = ""
    remember_tracks([{
        "videoId": video_id,
        "title": title,
        "artist": (props or {}).get("metadata/by-key/artist") or "",
        "album": (props or {}).get("metadata/by-key/album") or "",
        "duration": round(float((props or {}).get("duration") or 0)),
    }])
    print(json.dumps({"ok": True, "videoId": video_id}))


def cmd_play_next(args):
    if not args:
        fail("Usage: yt-music-ctl play-next <videoId> [title] [artist] [duration]")
    video_id = args[0]
    if not valid_video_id(video_id):
        fail("Invalid video ID")
    entry = track_from_args(video_id, args[1:])
    if not mpv_is_running():
        remember_tracks([entry])
        mpv_play(video_id)
        write_status_from_mpv(wait_for_metadata())
        print(json.dumps({"ok": True, "played": True, "videoId": video_id}))
        return
    url = watch_url(video_id)
    remember_tracks([entry])
    mpv_send("loadfile", [url, "insert-next"])
    print(json.dumps({"ok": True, "queuedNext": True, "videoId": video_id}))


def cmd_queue_add(args):
    if not args:
        fail("Usage: yt-music-ctl queue-add <videoId> [title] [artist] [duration]")
    video_id = args[0]
    if not valid_video_id(video_id):
        fail("Invalid video ID")
    entry = track_from_args(video_id, args[1:])
    if not mpv_is_running():
        remember_tracks([entry])
        mpv_play(video_id)
        write_status_from_mpv(wait_for_metadata())
        print(json.dumps({"ok": True, "played": True, "videoId": video_id}))
        return
    url = watch_url(video_id)
    remember_tracks([entry])
    mpv_send("loadfile", [url, "append"])
    print(json.dumps({"ok": True, "queued": True, "videoId": video_id}))


def _confirmed_ids(response, requested):
    """Video ids YouTube actually accepted, deduped and in response order.

    YouTube silently drops edit actions it will not apply (private, region
    locked, or otherwise unaddable videos) while still answering
    STATUS_SUCCEEDED, so the per-item results -- not the request -- decide.
    An action only counts when its result carries a setVideoId. Returns None
    when the response shape cannot be trusted, so the caller can verify
    against the playlist instead of assuming the whole request landed."""
    if not isinstance(response, dict):
        return None
    results = response.get("playlistEditResults")
    if not isinstance(results, list):
        return None
    confirmed = []
    for item in results:
        if not isinstance(item, dict):
            return None
        data = item.get("playlistEditVideoAddedResultData")
        if not isinstance(data, dict):
            data = item
        vid = data.get("videoId")
        if not vid:
            return None
        if data.get("setVideoId") and vid not in confirmed:
            confirmed.append(vid)
    return confirmed


def _add_video_ids(ytm, playlist_id, video_ids):
    """Add video ids to a playlist, skipping duplicates. Returns
    (added, duplicates, skipped, duplicate_ids, skipped_ids) where
    `duplicate_ids` names the ids already present in the playlist and
    `skipped_ids` names the ids YouTube refused to add even though the edit
    succeeded. Liked Music edits via song rating."""
    ids = []
    for vid in video_ids:
        if vid not in ids:
            ids.append(vid)
    added = 0
    duplicates = 0
    skipped = 0
    duplicate_ids = []
    skipped_ids = []
    if playlist_id == "LM":
        # Liked Music is a system playlist; edit it via the song rating.
        for vid in ids:
            ytm.rate_song(vid, "LIKE")
        added = len(ids)
    elif ids:
        # limit=None: a truncated page mis-detects duplicates and mis-resolves indices
        existing = ytm.get_playlist(playlist_id, limit=None)
        have = {t.get("videoId") for t in (existing.get("tracks") or [])}
        duplicate_ids = [vid for vid in ids if vid in have]
        fresh = [vid for vid in ids if vid not in have]
        duplicates = len(duplicate_ids)
        if fresh:
            response = ytm.add_playlist_items(playlist_id, fresh, duplicates=False)
            confirmed = _confirmed_ids(response, fresh)
            if confirmed is None:
                # Unrecognised edit response: verify against the playlist
                # itself rather than claiming the whole request landed.
                try:
                    after = ytm.get_playlist(playlist_id, limit=None)
                except Exception as e:
                    raise RuntimeError(
                        "playlist edit returned an unrecognised response and "
                        f"could not be verified: {e}") from e
                present = {t.get("videoId") for t in (after.get("tracks") or [])}
                added_ids = [vid for vid in fresh if vid in present]
                skipped_ids = [vid for vid in fresh if vid not in present]
            else:
                confirmed_set = set(confirmed)
                added_ids = [vid for vid in fresh if vid in confirmed_set]
                skipped_ids = [vid for vid in fresh if vid not in confirmed_set]
            added = len(added_ids)
            skipped = len(skipped_ids)
    if added > 0:
        invalidate_cache("playlist", [playlist_id])
    return added, duplicates, skipped, duplicate_ids, skipped_ids


def cmd_playlist_add(args):
    usage = "Usage: yt-music-ctl playlist-add <playlistId> <videoId...>"
    if len(args) < 2:
        fail(usage)
    playlist_id = args[0]
    ids = args[1:]
    if not all(valid_video_id(video_id) for video_id in ids):
        fail(usage)
    ytm = get_ytmusic()
    try:
        added, duplicates, skipped, duplicate_ids, skipped_ids = _add_video_ids(ytm, playlist_id, ids)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return
    print(json.dumps({"ok": True, "added": added, "duplicates": duplicates,
                      "skipped": skipped, "resolved": len(ids),
                      "playlistId": playlist_id,
                      "duplicateVideoIds": duplicate_ids,
                      "skippedVideoIds": skipped_ids}))


def cmd_playlist_add_items(args):
    usage = ("Usage: yt-music-ctl playlist-add-items <playlistId> "
             "<v:videoId|a:albumBrowseId|r:artistBrowseId|p:playlistId...>")
    if len(args) < 2:
        fail(usage)
    playlist_id = args[0]
    tokens = args[1:]
    # Validate every token's shape before touching the network. Every token
    # must carry one of the v:/a:/r:/p: prefixes so a stray word is a usage
    # error rather than an attempted (and confusing) YouTube lookup.
    for token in tokens:
        kind, sep, value = token.partition(":")
        if not sep or kind not in ("v", "a", "r", "p") or not value:
            fail(usage)
        if kind == "v" and not valid_video_id(value):
            fail(usage)
    ytm = get_ytmusic()
    try:
        resolved = []
        for token in tokens:
            if token.startswith("v:"):
                ids = [token[2:]]
            elif token.startswith("a:"):
                album = ytm.get_album(token[2:]) or {}
                tracks = album.get("tracks") if isinstance(album, dict) else []
                ids = [t.get("videoId") for t in (tracks or []) if isinstance(t, dict)]
            elif token.startswith("r:"):
                data = ytm.get_artist(token[2:]) or {}
                songs = data.get("songs") if isinstance(data, dict) else None
                songs = songs if isinstance(songs, dict) else {}
                rows = songs.get("results") or []
                ids = [t.get("videoId") for t in rows if isinstance(t, dict)]
            else:  # p:<playlistId>
                pl = ytm.get_playlist(token[2:], limit=None) or {}
                tracks = pl.get("tracks") if isinstance(pl, dict) else []
                ids = [t.get("videoId") for t in (tracks or []) if isinstance(t, dict)]
            for vid in ids:
                if valid_video_id(vid) and vid not in resolved:
                    resolved.append(vid)
        if not resolved:
            print(json.dumps({"ok": False, "error": "No playable tracks"}))
            return
        added, duplicates, skipped, duplicate_ids, skipped_ids = _add_video_ids(ytm, playlist_id, resolved)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return
    print(json.dumps({"ok": True, "added": added, "duplicates": duplicates,
                      "skipped": skipped, "resolved": len(resolved),
                      "playlistId": playlist_id,
                      "duplicateVideoIds": duplicate_ids,
                      "skippedVideoIds": skipped_ids}))


def cmd_playlist_edit(args):
    usage = ("Usage: yt-music-ctl playlist-edit <playlistId> [--title <text>] "
             "[--description <text>] [--privacy PUBLIC|PRIVATE|UNLISTED]")
    if not args:
        fail(usage)
    playlist_id = args[0]
    options = {}
    index = 1
    while index < len(args):
        flag = args[index]
        if flag not in ("--title", "--description", "--privacy"):
            fail(usage)
        if index + 1 >= len(args):
            fail(usage)
        options[flag] = args[index + 1]
        index += 2
    if not options:
        fail(usage)
    if ("--privacy" in options
            and options["--privacy"] not in ("PUBLIC", "PRIVATE", "UNLISTED")):
        fail(usage)
    kwargs = {}
    if "--title" in options:
        kwargs["title"] = options["--title"]
    if "--description" in options:
        kwargs["description"] = options["--description"]
    if "--privacy" in options:
        kwargs["privacyStatus"] = options["--privacy"]
    ytm = get_ytmusic()
    try:
        ytm.edit_playlist(playlist_id, **kwargs)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return
    invalidate_cache("playlist", [playlist_id])
    result = {"ok": True, "playlistId": playlist_id}
    for flag in ("--title", "--description", "--privacy"):
        if flag in options:
            result[flag[2:]] = options[flag]
    print(json.dumps(result))


def cmd_playlist_delete(args):
    if not args:
        fail("Usage: yt-music-ctl playlist-delete <playlistId>")
    playlist_id = args[0]
    if playlist_id == "LM":
        print(json.dumps({"ok": False, "error": "Cannot delete Liked Music"}))
        return
    ytm = get_ytmusic()
    try:
        ytm.delete_playlist(playlist_id)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return
    invalidate_cache("playlist", [playlist_id])
    print(json.dumps({"ok": True, "deleted": True, "playlistId": playlist_id}))


def cmd_playlist_move(args):
    usage = "Usage: yt-music-ctl playlist-move <playlistId> <fromIndex> <toIndex>"
    if len(args) != 3:
        fail(usage)
    playlist_id = args[0]
    try:
        source = int(args[1])
        target = int(args[2])
    except ValueError:
        fail(usage)
    if source < 0 or target < 0:
        fail(usage)
    ytm = get_ytmusic()
    try:
        tracks = [t for t in (ytm.get_playlist(playlist_id, limit=None).get("tracks") or [])
                  if t.get("videoId") and t.get("setVideoId")]
        if not (0 <= source < len(tracks)) or not (0 <= target < len(tracks)):
            print(json.dumps({"ok": False, "error": "Index out of range"}))
            return
        if source > target:
            # Moving up: the destination is always a valid successor.
            ytm.edit_playlist(playlist_id,
                              moveItem=(tracks[source]["setVideoId"],
                                        tracks[target]["setVideoId"]))
        elif source < target:
            # Moving down: swap each step against its neighbour so every call
            # has a real successor (a move to the last index has none).
            index = source
            for _ in range(target - source):
                ytm.edit_playlist(playlist_id,
                                  moveItem=(tracks[index + 1]["setVideoId"],
                                            tracks[index]["setVideoId"]))
                tracks[index], tracks[index + 1] = tracks[index + 1], tracks[index]
                index += 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return
    invalidate_cache("playlist", [playlist_id])
    print(json.dumps({"ok": True, "moved": True, "from": source, "to": target}))


def cmd_pause(args):
    if mpv_is_running():
        mpv_send("set_property", ["pause", True])
        props = get_mpv_props()
        write_status_from_mpv(props)
    print(json.dumps({"ok": True}))


def cmd_resume(args):
    if mpv_is_running():
        mpv_send("set_property", ["pause", False])
        props = get_mpv_props()
        write_status_from_mpv(props)
        print(json.dumps({"ok": True}))
        return
    # Idle: resume means start the saved queue up (never leave a silent no-op).
    restored, _info = _restore_session(pause=False)
    if restored:
        print(json.dumps({"ok": True, "resumed": True}))
    else:
        print(json.dumps({"ok": True}))


def cmd_toggle(args):
    if not mpv_is_running():
        # Nothing to toggle: start the saved queue playing again, at the
        # position it was at before playback stopped (radio included, since a
        # live station is deliberately not saved and therefore falls back to
        # the last song queue). No session at all keeps the old failure.
        restored, _info = _restore_session(pause=False)
        if not restored:
            fail("Nothing playing")
        print(json.dumps({"ok": True, "resumed": True}))
        return
    props = get_mpv_props()
    if props:
        paused = props.get("pause", True)
        mpv_send("set_property", ["pause", not paused])
        props2 = get_mpv_props()
        write_status_from_mpv(props2)


def cmd_next(args):
    if not mpv_is_running():
        fail("Nothing playing")
    before = get_mpv_props()
    mpv_send("playlist-next", "force")
    if before:
        props = wait_for_track_change(before.get("path"), before.get("playlist-pos"))
    else:
        time.sleep(0.3)
        props = get_mpv_props()
    write_status_from_mpv(props)
    prune_radio_if_moved_on()
    print(json.dumps({"ok": True}))


def cmd_prev(args):
    if not mpv_is_running():
        fail("Nothing playing")
    before = get_mpv_props()
    mpv_send("playlist-prev", "force")
    if before:
        props = wait_for_track_change(before.get("path"), before.get("playlist-pos"))
    else:
        time.sleep(0.3)
        props = get_mpv_props()
    write_status_from_mpv(props)
    prune_radio_if_moved_on()
    print(json.dumps({"ok": True}))


def cmd_seek(args):
    if not args:
        fail("Usage: yt-music-ctl seek <seconds>")
    try:
        seconds = int(args[0])
    except (TypeError, ValueError):
        fail("Usage: yt-music-ctl seek <seconds>")
    if not mpv_is_running():
        fail("Nothing playing")
    mpv_send("seek", [seconds, "relative"])
    props = get_mpv_props()
    write_status_from_mpv(props)
    print(json.dumps({"ok": True}))


def cmd_like(args):
    if not args:
        fail("Usage: yt-music-ctl like <videoId>")
    video_id = args[0]
    if not valid_video_id(video_id):
        fail("Invalid video ID")
    ytm = get_ytmusic()
    try:
        # Rating the song is enough: YouTube adds it to Liked Music itself.
        # The previous explicit add_playlist_items call was redundant and made
        # YouTube answer HTTP 400 ("invalid argument"), which surfaced as a
        # bogus error payload even though the like had worked.
        ytm.rate_song(video_id, "LIKE")
        invalidate_namespace("liked")
        print(json.dumps({"ok": True, "liked": True}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_dislike(args):
    if not args:
        fail("Usage: yt-music-ctl dislike <videoId>")
    video_id = args[0]
    ytm = get_ytmusic()

    rated = False
    removed = []
    errors = []
    try:
        ytm.rate_song(video_id, "INDIFFERENT")
        rated = True
    except Exception as e:
        errors.append(f"rating: {e}")

    # Only inspect owned playlists: YouTube does not allow removing items
    # from playlists owned by someone else.
    try:
        playlists = ytm.get_library_playlists(limit=100)
        for playlist in playlists:
            if not playlist.get("owned"):
                continue
            playlist_id = playlist.get("playlistId", "")
            if not playlist_id:
                continue
            try:
                tracks = ytm.get_playlist(playlist_id, limit=None).get("tracks") or []
                matches = [
                    {"videoId": track["videoId"], "setVideoId": track["setVideoId"]}
                    for track in tracks
                    if track.get("videoId") == video_id and track.get("setVideoId")
                ]
                if matches:
                    ytm.remove_playlist_items(playlist_id, matches)
                    removed.append(playlist.get("title", playlist_id))
            except Exception as e:
                errors.append(f"{playlist.get('title', playlist_id)}: {e}")
    except Exception as e:
        errors.append(f"playlists: {e}")

    # Advance even if a playlist edit fails, so dislike always skips playback.
    if mpv_is_running():
        mpv_send("playlist-next", "force")

    if rated:
        # A dislike changes the rating and can remove the track from every
        # owned playlist, so drop the cached screens that would still show it.
        invalidate_namespace("liked")
        invalidate_namespace("playlist")
        invalidate_namespace("library")

    result = {"ok": rated, "disliked": rated, "removedFrom": removed, "skipped": True}
    if errors:
        result["errors"] = errors
    print(json.dumps(result))


def cmd_unlike(args):
    if not args:
        fail("Usage: yt-music-ctl unlike <videoId>")
    video_id = args[0]
    ytm = get_ytmusic()
    try:
        ytm.rate_song(video_id, "INDIFFERENT")
        invalidate_namespace("liked")
        print(json.dumps({"ok": True}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def _library_playlists(ytm):
    """Map the account's library playlists to the JSON shape we surface."""
    playlists = ytm.get_library_playlists(limit=50)
    result = []
    for pl in playlists:
        # `count` is the real track count for owned playlists but absent
        # for system/auto playlists (Liked Music, radio mixes, episodes),
        # so it is reported as null rather than a bogus number.
        raw_count = str(pl.get("count") or "").strip()
        result.append({
            "id": pl.get("playlistId", ""),
            "title": pl.get("title", ""),
            "count": int(raw_count) if raw_count.isdigit() else None,
            "description": pl.get("description", ""),
        })
    return result


def cmd_playlists(args):
    ytm = get_ytmusic()
    try:
        result = _library_playlists(ytm)
        if not result and _auth_marker_fresh():
            # Anonymous access looks like an empty library. Force a real
            # revalidation (refreshing from browser cookies) and retry once.
            _clear_auth_marker()
            ytm = get_ytmusic(force_auth=True)
            result = _library_playlists(ytm)
        print(json.dumps({"ok": True, "playlists": result}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_create_playlist(args):
    if not args:
        fail("Usage: yt-music-ctl create-playlist <name>")
    ids = []
    if len(args) >= 2 and all(valid_video_id(video_id) for video_id in args[1:]):
        title = args[0]
        ids = args[1:]
    else:
        title = " ".join(args).strip()
    if not title:
        fail("Playlist name cannot be empty")
    ytm = get_ytmusic()
    try:
        playlist_id = ytm.create_playlist(title, "", "PRIVATE",
                                          video_ids=ids or None)
        if isinstance(playlist_id, dict):
            playlist_id = playlist_id.get("playlistId", "")
        print(json.dumps({"ok": True, "id": playlist_id, "title": title,
                          "tracks": len(ids)}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_playlist_tracks(args):
    args, refresh = _strip_refresh(args)
    if not args:
        fail("Usage: yt-music-ctl playlist <playlistId>")
    playlist_id = args[0]
    key = [playlist_id]
    ttl = METADATA_CACHE_TTL["playlist"]
    if not refresh:
        payload, fresh = cache_read("playlist", key, ttl)
        if fresh and payload is not None:
            print(json.dumps(_cache_served(payload)))
            return
        if payload is not None:
            # Stale but usable: serve it now, refresh in the background.
            print(json.dumps(_cache_served(payload, stale=True)))
            spawn_background_refresh("playlist", [playlist_id])
            return
    ytm = _ytmusic_for_cache("playlist", key, ttl, force_auth=refresh)
    if ytm is None:
        return
    try:
        pl = ytm.get_playlist(playlist_id, limit=100)
        tracks = []
        for track in (pl.get("tracks") or []):
            vid = track.get("videoId", "")
            if not vid:
                continue
            tracks.append({
                "videoId": vid,
                "setVideoId": track.get("setVideoId", ""),
                "title": track.get("title", ""),
                "artist": ", ".join(a.get("name", "") for a in (track.get("artists") or [])),
                "album": track.get("album", {}).get("title", "") if track.get("album") else "",
                "duration": track.get("duration_seconds", 0) or 0,
                "thumbnail": (track.get("thumbnails", [{}])[-1].get("url", "")
                              if track.get("thumbnails") else ""),
            })
        payload = {
            "ok": True,
            "title": pl.get("title", ""),
            "playlistId": playlist_id,
            "tracks": tracks,
            "cached": False,
        }
        cache_write("playlist", key, payload)
        print(json.dumps(payload))
    except Exception as e:
        if not _serve_stale("playlist", key, ttl):
            print(json.dumps({"ok": False, "error": str(e)}))


def cmd_remove(args):
    if len(args) < 2:
        fail("Usage: yt-music-ctl remove <playlistId> <videoId>")
    playlist_id, video_id = args[0], args[1]
    ytm = get_ytmusic()
    try:
        # Liked Music is a YouTube system playlist, so it must be edited by
        # changing the song rating rather than with browse/edit_playlist.
        if playlist_id == "LM":
            ytm.rate_song(video_id, "INDIFFERENT")
            invalidate_cache("playlist", [playlist_id])
            print(json.dumps({"ok": True, "removed": 1}))
            return

        playlist = ytm.get_playlist(playlist_id, limit=None)
        matches = [
            {"videoId": track["videoId"], "setVideoId": track["setVideoId"]}
            for track in (playlist.get("tracks") or [])
            if track.get("videoId") == video_id and track.get("setVideoId")
        ]
        if not matches:
            print(json.dumps({"ok": False, "error": "Track is not removable from this playlist"}))
            return
        ytm.remove_playlist_items(playlist_id, matches)
        invalidate_cache("playlist", [playlist_id])
        print(json.dumps({"ok": True, "removed": len(matches)}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def song_row(track):
    """Normalize a ytmusicapi track dict into the plugin's song shape."""
    album = track.get("album") or {}
    if not isinstance(album, dict):
        album = {"name": album}
    thumbs = track.get("thumbnails") or []
    return {
        "kind": "song",
        "videoId": track.get("videoId", ""),
        "title": track.get("title", ""),
        "artist": ", ".join(a.get("name", "") for a in (track.get("artists") or [])),
        "album": album.get("title", "") or album.get("name", ""),
        "duration": track.get("duration_seconds", 0) or 0,
        "thumbnail": thumbs[-1].get("url", "") if thumbs else "",
    }


def library_album_row(album):
    thumbs = album.get("thumbnails") or []
    return {
        "kind": "album",
        "browseId": album.get("browseId", ""),
        "title": album.get("title", ""),
        "artist": ", ".join(a.get("name", "") for a in (album.get("artists") or [])),
        "year": album.get("year", "") or "",
        "thumbnail": thumbs[-1].get("url", "") if thumbs else "",
    }


def library_artist_row(artist):
    thumbs = artist.get("thumbnails") or []
    name = artist.get("artist", "") or artist.get("name", "")
    return {
        "kind": "artist",
        "browseId": artist.get("browseId", ""),
        "name": name,
        "title": name,
        "subscribers": artist.get("subscribers", "") or "",
        "thumbnail": thumbs[-1].get("url", "") if thumbs else "",
    }


def playlist_row(p):
    return {
        "kind": "playlist",
        "videoId": "",
        "browseId": p.get("playlistId") or p.get("browseId") or "",
        "title": p.get("title", "") or "",
        "artist": p.get("author", "") or "",
        "duration": 0,
    }


def mixed_row(item):
    """Map a heterogeneous ytmusicapi item to the generic item shape."""
    if not isinstance(item, dict):
        return None
    if item.get("videoId"):
        return song_row(item)
    browse_id = str(item.get("browseId", ""))
    # Album/single/EP pages share the MPREb_ browse id prefix; search results
    # label singles as type "Single" and omit audioPlaylistId, so the prefix is
    # the reliable signal here.
    if (item.get("audioPlaylistId") or item.get("type") in ("Album", "Single", "EP")
            or browse_id.startswith("MPREb_")):
        return library_album_row(item)
    if item.get("subscribers") is not None or browse_id.startswith("UC"):
        row = library_artist_row(item)
        row["title"] = item.get("artist") or item.get("title") or ""
        return row
    pid = item.get("playlistId") or item.get("browseId")
    if pid:
        return playlist_row(item)
    return None


def cmd_liked(args):
    args, refresh = _strip_refresh(args)
    limit = 100
    if args:
        try:
            limit = max(1, min(300, int(args[0])))
        except ValueError:
            fail("Usage: yt-music-ctl liked [limit]")
    key = [limit]
    ttl = METADATA_CACHE_TTL["liked"]
    if not refresh:
        payload, fresh = cache_read("liked", key, ttl)
        if fresh and payload is not None:
            print(json.dumps(_cache_served(payload)))
            return
        if payload is not None:
            # Stale but usable: serve it now, refresh in the background.
            print(json.dumps(_cache_served(payload, stale=True)))
            spawn_background_refresh("liked", [limit])
            return
    ytm = _ytmusic_for_cache("liked", key, ttl, force_auth=refresh)
    if ytm is None:
        return
    try:
        liked = ytm.get_liked_songs(limit=limit)
        items = [song_row(t) for t in (liked.get("tracks") or []) if t.get("videoId")]
        payload = {
            "ok": True,
            "title": liked.get("title", "Liked Music"),
            "playlistId": "LM",
            "items": items,
            "cached": False,
        }
        cache_write("liked", key, payload)
        print(json.dumps(payload))
    except Exception as e:
        if not _serve_stale("liked", key, ttl):
            print(json.dumps({"ok": False, "error": str(e)}))


def cmd_library(args):
    args, refresh = _strip_refresh(args)
    if not args or args[0] not in ("songs", "albums", "artists", "playlists"):
        fail("Usage: yt-music-ctl library <songs|albums|artists|playlists> [limit]")
    kind = args[0]
    limit = 100
    if len(args) > 1:
        try:
            limit = max(1, min(500, int(args[1])))
        except ValueError:
            fail("Invalid limit")
    key = [kind, limit]
    ttl = METADATA_CACHE_TTL["library"]
    if not refresh:
        payload, fresh = cache_read("library", key, ttl)
        if fresh and payload is not None:
            print(json.dumps(_cache_served(payload)))
            return
        if payload is not None:
            # Stale but usable: serve it now, refresh in the background.
            print(json.dumps(_cache_served(payload, stale=True)))
            spawn_background_refresh("library", [kind, limit])
            return
    ytm = _ytmusic_for_cache("library", key, ttl, force_auth=refresh)
    if ytm is None:
        return
    try:
        if kind == "songs":
            rows = ytm.get_library_songs(limit=limit) or []
            items = [song_row(t) for t in rows if t.get("videoId")]
        elif kind == "albums":
            rows = ytm.get_library_albums(limit=limit) or []
            items = [library_album_row(a) for a in rows if a.get("browseId")]
        elif kind == "artists":
            rows = ytm.get_library_artists(limit=limit) or []
            items = [library_artist_row(a) for a in rows if a.get("browseId")]
        else:  # playlists
            rows = ytm.get_library_playlists(limit=limit) or []
            items = [playlist_row(p) for p in rows if p.get("playlistId")]
        payload = {"ok": True, "kind": kind, "items": items, "cached": False}
        cache_write("library", key, payload)
        print(json.dumps(payload))
    except Exception as e:
        if not _serve_stale("library", key, ttl):
            print(json.dumps({"ok": False, "kind": kind, "error": str(e)}))


def cmd_album(args):
    args, refresh = _strip_refresh(args)
    if not args:
        fail("Usage: yt-music-ctl album <browseId>")
    browse_id = args[0]
    key = [browse_id]
    ttl = METADATA_CACHE_TTL["album"]
    if not refresh:
        payload, fresh = cache_read("album", key, ttl)
        if fresh and payload is not None:
            print(json.dumps(_cache_served(payload)))
            return
        if payload is not None:
            # Stale but usable: serve it now, refresh in the background.
            print(json.dumps(_cache_served(payload, stale=True)))
            spawn_background_refresh("album", [browse_id])
            return
    ytm = _ytmusic_for_cache("album", key, ttl, force_auth=refresh)
    if ytm is None:
        return
    try:
        album = ytm.get_album(browse_id)
        thumbs = album.get("thumbnails") or []
        duration_seconds = int(album.get("duration_seconds") or 0)
        payload = {
            "ok": True,
            "browseId": browse_id,
            "title": album.get("title", ""),
            "artist": ", ".join(a.get("name", "") for a in (album.get("artists") or [])),
            "year": album.get("year", "") or "",
            "audioPlaylistId": album.get("audioPlaylistId", ""),
            "thumbnail": thumbs[-1].get("url", "") if thumbs else "",
            "type": album.get("type", "") or "",
            "trackCount": int(album.get("trackCount") or 0),
            "durationSeconds": duration_seconds,
            "duration": album.get("duration") or format_duration(duration_seconds),
            "description": album.get("description", "") or "",
            "explicit": bool(album.get("isExplicit")),
            "items": [song_row(t) for t in (album.get("tracks") or []) if t.get("videoId")],
            "cached": False,
        }
        cache_write("album", key, payload)
        print(json.dumps(payload))
    except Exception as e:
        if not _serve_stale("album", key, ttl):
            print(json.dumps({"ok": False, "error": str(e)}))


_MISSING = object()


def _first_by_key(node, key):
    """Depth-first lookup of the first dict carrying `key` anywhere below
    `node`. Returns _MISSING when the payload has no such key, so a present
    but falsy value (isToggled: false) is never mistaken for "absent"."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _first_by_key(value, key)
            if found is not _MISSING:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _first_by_key(item, key)
            if found is not _MISSING:
                return found
    return _MISSING


def _album_in_library(response):
    """Saved-to-library flag from a raw album browse response.

    The album header exposes it as `isToggled` on its toggle button; prefer
    the button inside `musicResponsiveHeaderRenderer` (the album page's own
    header) and fall back to the first toggle button anywhere in the payload.
    Returns None when the response carries no such button at all."""
    for container_key in ("musicResponsiveHeaderRenderer", None):
        scope = response if container_key is None else _first_by_key(response, container_key)
        if scope is _MISSING or scope is None:
            continue
        toggle = _first_by_key(scope, "toggleButtonRenderer")
        if isinstance(toggle, dict) and "isToggled" in toggle:
            return bool(toggle["isToggled"])
    return None


def cmd_album_status(args):
    if not args:
        fail("Usage: yt-music-ctl album-status <browseId>")
    browse_id = args[0]
    ytm = get_ytmusic()
    try:
        resp = ytm._send_request("browse", {"browseId": browse_id})
        print(json.dumps({"ok": True, "inLibrary": _album_in_library(resp) is True}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def _set_album_library(browse_id, saved):
    """Like/unlike an album: rate_playlist is YouTube Music's own
    "Add to library / Remove from library" interaction for albums."""
    ytm = get_ytmusic()
    try:
        from ytmusicapi import LikeStatus
        album = ytm.get_album(browse_id)
        apid = album.get("audioPlaylistId")
        if not apid:
            print(json.dumps({"ok": False, "error": "No audio playlist for this album"}))
            return
        ytm.rate_playlist(apid, LikeStatus.LIKE if saved else LikeStatus.INDIFFERENT)
        # The cached `library albums` list would otherwise stay stale for the
        # whole TTL and hide/show the album until it expired.
        invalidate_namespace("library")
        print(json.dumps({"ok": True, "saved": bool(saved)}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_album_save(args):
    if not args:
        fail("Usage: yt-music-ctl album-save <browseId>")
    _set_album_library(args[0], True)


def cmd_album_remove(args):
    if not args:
        fail("Usage: yt-music-ctl album-remove <browseId>")
    _set_album_library(args[0], False)


def cmd_artist(args):
    args, refresh = _strip_refresh(args)
    if not args:
        fail("Usage: yt-music-ctl artist <browseId>")
    browse_id = args[0]
    key = [browse_id]
    ttl = METADATA_CACHE_TTL["artist"]
    if not refresh:
        payload, fresh = cache_read("artist", key, ttl)
        if fresh and payload is not None:
            print(json.dumps(_cache_served(payload)))
            return
        if payload is not None:
            # Stale but usable: serve it now, refresh in the background.
            print(json.dumps(_cache_served(payload, stale=True)))
            spawn_background_refresh("artist", [browse_id])
            return
    ytm = _ytmusic_for_cache("artist", key, ttl, force_auth=refresh)
    if ytm is None:
        return
    try:
        artist = ytm.get_artist(browse_id)
        songs = ((artist.get("songs") or {}).get("results") or [])
        albums = ((artist.get("albums") or {}).get("results") or [])
        singles = ((artist.get("singles") or {}).get("results") or [])
        thumbs = artist.get("thumbnails") or []
        top_songs = [song_row(t) for t in songs if t.get("videoId")]
        album_rows = [library_album_row(a) for a in albums if a.get("browseId")]
        single_rows = [library_album_row(s) for s in singles if s.get("browseId")]
        related = artist.get("related")
        rel_results = related if isinstance(related, list) else ((related or {}).get("results") or [])
        similar = []
        for r in rel_results:
            if not isinstance(r, dict) or not r.get("browseId"):
                continue
            r_thumbs = r.get("thumbnails") or []
            similar.append({
                "kind": "artist",
                "browseId": r.get("browseId", ""),
                "title": r.get("title", "") or r.get("name", ""),
                "artist": "",
                "subscribers": r.get("subscribers", "") or "",
                "thumbnail": r_thumbs[-1].get("url", "") if r_thumbs else "",
            })
        payload = {
            "ok": True,
            "browseId": browse_id,
            "name": artist.get("name", ""),
            "subscribers": artist.get("subscribers", "") or "",
            "description": artist.get("description", "") or "",
            "thumbnail": thumbs[-1].get("url", "") if thumbs else "",
            "views": artist.get("views", "") or "",
            "monthlyListeners": artist.get("monthlyListeners", "") or "",
            "topSongs": top_songs,
            "albums": album_rows,
            "singles": single_rows,
            "items": top_songs + album_rows + single_rows,
            "similar": similar,
            "radioId": artist.get("radioId", "") or "",
            "shuffleId": artist.get("shuffleId", "") or "",
            "cached": False,
        }
        cache_write("artist", key, payload)
        print(json.dumps(payload))
    except Exception as e:
        if not _serve_stale("artist", key, ttl):
            print(json.dumps({"ok": False, "error": str(e)}))


def cmd_artist_radio(args):
    args, refresh = _strip_refresh(args)
    ensure_daemon()
    if not args:
        fail("Usage: yt-music-ctl radio <browseId>")
    browse_id = args[0]
    key = [browse_id]
    ttl = METADATA_CACHE_TTL["radio"]
    if not refresh:
        payload, fresh = cache_read("radio", key, ttl)
        track_list = payload.get("trackList") if isinstance(payload, dict) else None
        if fresh and isinstance(track_list, list) and track_list:
            # Metadata comes from the cache, playback still happens here.
            _mix_launch(track_list)
            print(json.dumps(_cache_served(_mix_output(payload))))
            return
        if isinstance(track_list, list) and track_list:
            # Stale but usable: start playback now, refresh in the background.
            print(json.dumps(_cache_served(_mix_output(payload), stale=True)))
            spawn_background_refresh("radio", [browse_id])
            _mix_launch(track_list)
            return
    ytm = _ytmusic_for_cache("radio", key, ttl, force_auth=refresh)
    if ytm is None:
        return
    try:
        artist = ytm.get_artist(browse_id)
        radio_id = artist.get("radioId", "") or ""
        if not radio_id:
            print(json.dumps({"ok": False, "error": "No radio for this artist"}))
            return
        watchlist = ytm.get_watch_playlist(playlistId=radio_id, radio=True)
        tracks = []
        for track in (watchlist.get("tracks") or []):
            vid = track.get("videoId", "")
            if not vid:
                continue
            album = track.get("album") or {}
            if not isinstance(album, dict):
                album = {"name": album}
            tracks.append({
                "videoId": vid,
                "title": track.get("title", ""),
                "artist": ", ".join(a.get("name", "") for a in (track.get("artists") or [])),
                "album": album.get("title", "") or album.get("name", ""),
                "duration": track.get("duration_seconds", 0) or 0,
            })
        if not tracks:
            print(json.dumps({"ok": False, "error": "No tracks returned"}))
            return
        payload = {"ok": True, "mix": True, "seedId": browse_id,
                   "radioId": radio_id, "trackList": tracks, "cached": False}
        cache_write("radio", key, payload)
        _mix_launch(tracks)
        print(json.dumps(_mix_output(payload)))
    except Exception as e:
        if not _serve_stale("radio", key, ttl):
            print(json.dumps({"ok": False, "error": str(e)}))


def cmd_search(args):
    args, refresh = _strip_refresh(args)
    search_filters = ("songs", "albums", "artists", "playlists")
    if args and args[0] == "-f":
        if len(args) < 2 or args[1] not in search_filters:
            fail("Invalid search filter")
        filter_kind = args[1]
        query = " ".join(args[2:])
    else:
        filter_kind = "songs"
        query = " ".join(args)
    query = " ".join(query.split())
    if not query:
        fail("Usage: yt-music-ctl search [-f songs|albums|artists|playlists] <query>")
    expected = {"songs": "song", "albums": "album",
                "artists": "artist", "playlists": "playlist"}[filter_kind]
    key = [filter_kind, query]
    ttl = METADATA_CACHE_TTL["search"]
    if not refresh:
        payload, fresh = cache_read("search", key, ttl)
        if fresh and payload is not None:
            print(json.dumps(_cache_served(payload)))
            return
        if payload is not None:
            # Stale but usable: serve it now, refresh in the background.
            # Re-passed as CLI-style -f args so the child rebuilds this key.
            print(json.dumps(_cache_served(payload, stale=True)))
            spawn_background_refresh("search", ["-f", filter_kind] + query.split())
            return
    ytm = _ytmusic_for_cache("search", key, ttl, require_auth=False)
    if ytm is None:
        return
    try:
        results = ytm.search(query, filter=filter_kind, limit=20)
        items = []
        for r in results:
            if r.get("resultType") != expected:
                continue
            row = mixed_row(r)
            if row:
                items.append(row)
            if len(items) >= 50:
                break
        payload = {"ok": True, "query": query, "filter": filter_kind,
                   "items": items, "cached": False}
        cache_write("search", key, payload)
        print(json.dumps(payload))
    except Exception as e:
        if not _serve_stale("search", key, ttl):
            print(json.dumps({"ok": False, "error": str(e)}))


def cmd_home(args):
    args, refresh = _strip_refresh(args)
    sections = 3
    if args:
        try:
            sections = max(1, min(8, int(args[0])))
        except ValueError:
            fail("Usage: yt-music-ctl home [sections]")
    key = [sections]
    ttl = METADATA_CACHE_TTL["home"]
    if not refresh:
        payload, fresh = cache_read("home", key, ttl)
        if fresh and payload is not None:
            print(json.dumps(_cache_served(payload)))
            return
        if payload is not None:
            # Stale but usable: serve it now, refresh in the background.
            print(json.dumps(_cache_served(payload, stale=True)))
            spawn_background_refresh("home", [sections])
            return
    ytm = _ytmusic_for_cache("home", key, ttl, force_auth=refresh)
    if ytm is None:
        return
    try:
        feed = ytm.get_home(limit=sections) or []
        items = []
        for section in feed:
            for item in (section.get("contents") or []):
                row = mixed_row(item)
                if row:
                    items.append(row)
                if len(items) >= 200:
                    break
            if len(items) >= 200:
                break
        payload = {"ok": True, "items": items, "cached": False}
        cache_write("home", key, payload)
        print(json.dumps(payload))
    except Exception as e:
        if not _serve_stale("home", key, ttl):
            print(json.dumps({"ok": False, "error": str(e)}))


def cmd_station_catalog(args):
    """List the editable Featured stations, merged over the embedded catalog.

    Starts from the user's added/hidden deltas and appends every remaining
    catalog row, so the list is user-editable yet still benefits from future
    catalog updates. Offline; -r is ignored.
    """
    _args, _refresh = _strip_refresh(args)
    print(json.dumps({"ok": True, "items": _featured_items()}))


def cmd_station_search(args):
    args, refresh = _strip_refresh(args)
    tag_mode = False
    limit = 30
    query_parts = []
    index = 0
    while index < len(args):
        if args[index] == "--tag":
            tag_mode = True
            index += 1
            continue
        if args[index] == "--limit":
            # A bare trailing --limit (or a non-integer value) is a usage error
            # rather than leaking the flag into the query string.
            if index + 1 >= len(args):
                print(json.dumps({"ok": False, "error":
                                  "Usage: yt-music-ctl station-search [--tag] "
                                  "<query> [--limit N]"}))
                return
            try:
                limit = int(args[index + 1])
            except (TypeError, ValueError):
                print(json.dumps({"ok": False, "error":
                                  "Usage: yt-music-ctl station-search [--tag] "
                                  "<query> [--limit N]"}))
                return
            limit = max(1, min(100, limit))
            index += 2
            continue
        query_parts.append(args[index])
        index += 1
    query = " ".join(query_parts).strip()
    if not query:
        print(json.dumps({"ok": False, "error":
                          "Usage: yt-music-ctl station-search [--tag] "
                          "<query> [--limit N]"}))
        return
    key = ["search", "tag" if tag_mode else "name", query.lower(), limit]
    ttl = METADATA_CACHE_TTL["stations"]
    if not refresh:
        payload, fresh = cache_read("stations", key, ttl)
        if fresh and payload is not None:
            print(json.dumps(_cache_served(payload)))
            return
    try:
        rows = _radio_browser_request("/json/stations/search", {
            "tag" if tag_mode else "name": query,
            "hidebroken": "true",
            "order": "clickcount",
            "reverse": "true",
            "limit": str(limit),
        })
        items = []
        for raw in rows or []:
            row = normalize_station(raw, "radio-browser")
            if row:
                items.append(row)
            if len(items) >= limit:
                break
        payload = {"ok": True, "items": items, "cached": False}
        cache_write("stations", key, payload)
        print(json.dumps(payload))
    except Exception as e:
        if not _serve_stale("stations", key, ttl):
            print(json.dumps({"ok": False, "error": str(e)}))


def cmd_station_favorites(args):
    """List locally saved radio stations. Offline; -r is ignored."""
    _args, _refresh = _strip_refresh(args)
    print(json.dumps({"ok": True, "items": load_radio_stations()}))


def cmd_station_fav_add(args):
    """Save one station, from a JSON record or a bare stream URL plus name."""
    if not args:
        print(json.dumps({"ok": False, "error":
                          "Usage: yt-music-ctl station-fav-add "
                          "'<json>|<url> [name]'"}))
        return
    if args[0].lstrip().startswith("{"):
        try:
            raw = json.loads(args[0])
        except (TypeError, ValueError):
            print(json.dumps({"ok": False, "error": "Invalid station JSON"}))
            return
        source = "favorite"
    else:
        raw = {"url": args[0], "name": " ".join(args[1:]).strip()}
        source = "user"
    row = normalize_station(raw, source)
    if not row:
        print(json.dumps({"ok": False, "error": "Invalid station URL"}))
        return
    _ensure_station_id(row)
    updated = []
    replaced = False
    for entry in load_radio_stations():
        if entry.get("id") == row["id"]:
            updated.append(row)
            replaced = True
        else:
            updated.append(entry)
    if not replaced:
        # save_radio_stations keeps the first RADIO_STATIONS_MAX rows, so a new
        # station appended at the cap would be silently dropped while we still
        # reported success. Evict the oldest entry first so the newest survives.
        if len(updated) >= RADIO_STATIONS_MAX:
            updated = updated[-(RADIO_STATIONS_MAX - 1):]
        updated.append(row)
    save_radio_stations(updated)
    print(json.dumps({"ok": True, "added": True, "id": row["id"],
                      "count": len(load_radio_stations())}))


def cmd_station_fav_remove(args):
    """Drop one saved station by id. Offline."""
    args, _refresh = _strip_refresh(args)
    if not args:
        print(json.dumps({"ok": False, "error":
                          "Usage: yt-music-ctl station-fav-remove <id>"}))
        return
    target = args[0]
    items = load_radio_stations()
    remaining = [e for e in items if e.get("id") != target]
    removed = len(remaining) != len(items)
    save_radio_stations(remaining)
    print(json.dumps({"ok": True, "removed": removed,
                      "count": len(load_radio_stations())}))


def cmd_station_featured_add(args):
    """Add one station to the Featured list, from JSON or a bare URL + name.

    Adding also unhides a previously hidden catalog station. A custom station
    is prepended; a catalog station is left to the catalog pass so it is not
    duplicated in the stored deltas.
    """
    if not args:
        print(json.dumps({"ok": False, "error":
                          "Usage: yt-music-ctl station-featured-add "
                          "'<json>|<url> [name]'"}))
        return
    if args[0].lstrip().startswith("{"):
        try:
            raw = json.loads(args[0])
        except (TypeError, ValueError):
            print(json.dumps({"ok": False, "error": "Invalid station JSON"}))
            return
        source = "favorite"
    else:
        raw = {"url": args[0], "name": " ".join(args[1:]).strip()}
        source = "user"
    row = normalize_station(raw, source)
    if not row:
        print(json.dumps({"ok": False, "error": "Invalid station URL"}))
        return
    _ensure_station_id(row)
    state = load_featured()
    added = [e for e in state["added"] if e.get("id") != row["id"]]
    hidden = [h for h in state["hidden"] if h != row["id"]]
    if row["id"] not in _catalog_featured_ids():
        # Keep the newest addition when at the cap: _featured_items walks the
        # stored order, so trim the oldest tail before prepending.
        if len(added) >= RADIO_STATIONS_MAX:
            added = added[-(RADIO_STATIONS_MAX - 1):]
        added.insert(0, row)
    save_featured(added, hidden)
    print(json.dumps({"ok": True, "added": True, "id": row["id"],
                      "count": len(_featured_items())}))


def cmd_station_featured_remove(args):
    """Remove one station from the Featured list by id.

    A user addition is dropped outright; a catalog station is hidden so the
    embedded row does not reappear on the next read. Offline.
    """
    args, _refresh = _strip_refresh(args)
    if not args:
        print(json.dumps({"ok": False, "error":
                          "Usage: yt-music-ctl station-featured-remove <id>"}))
        return
    target = args[0]
    state = load_featured()
    added = [e for e in state["added"] if e.get("id") != target]
    hidden = list(state["hidden"])
    if target in _catalog_featured_ids() and target not in hidden:
        hidden.append(target)
    save_featured(added, hidden)
    print(json.dumps({"ok": True, "removed": True, "id": target,
                      "count": len(_featured_items())}))


def _radio_mpv_argv(url):
    """mpv argv for one live stream. Pure, so the smoke test can inspect it."""
    return ["mpv", "--no-video", "--really-quiet",
            f"--input-ipc-server={MPV_SOCKET}", "--keep-open=no",
            "--network-timeout=30",
            "--stream-lavf-o=reconnect=1,reconnect_streamed=1"] \
        + mpv_volume_args() + [url]


def cmd_station_play(args):
    """Play one internet radio station through the shared mpv pipeline.

    The station is marked live first, so the status daemon stamps it into the
    shared status file without recording YouTube history, session, sidecar or
    precache. Accepts a JSON record, a bare stream URL plus name, or a saved
    station id.
    """
    args, _refresh = _strip_refresh(args)
    if not args:
        print(json.dumps({"ok": False, "error":
                          "Usage: yt-music-ctl station-play "
                          "'<json>|<url>|<id>' [name]"}))
        return
    if args[0].lstrip().startswith("{"):
        try:
            raw = json.loads(args[0])
        except (TypeError, ValueError):
            print(json.dumps({"ok": False, "error": "Invalid station JSON"}))
            return
        source = "favorite"
    elif args[0].startswith("http://") or args[0].startswith("https://"):
        raw = {"url": args[0], "name": " ".join(args[1:]).strip()}
        source = "user"
    else:
        raw = None
        for entry in load_radio_stations():
            if entry.get("id") == args[0]:
                raw = entry
                break
        if raw is None:
            print(json.dumps({"ok": False, "error": "Unknown station"}))
            return
        source = "favorite"
    row = normalize_station(raw, source)
    if not row:
        print(json.dumps({"ok": False, "error": "Invalid station URL"}))
        return
    _ensure_station_id(row)
    ensure_daemon()
    if mpv_is_running():
        # Pin the station at the TOP of the queue and play it. Capture the old
        # marker before stamping the new one: if the loadfile never inserts an
        # entry we must restore it, otherwise the old station would keep
        # playing under the new station's name.
        old_marker = load_radio_current()
        set_radio_current(row)
        before = mpv_query(["path", "playlist-pos", "playlist-count"]) or {}
        try:
            prev_count = int(before.get("playlist-count") or 0)
        except (TypeError, ValueError):
            prev_count = 0
        mpv_send("loadfile", [row["url"], "insert-at", "0"])
        # The loadfile is async: wait briefly until the entry exists. If the
        # count never grows mpv rejected the URL, so treat that as a failure
        # rather than playing whatever entry is already at index 0.
        loaded = False
        deadline = time.time() + 2.0
        while time.time() < deadline:
            query = mpv_query(["playlist-count"]) or {}
            try:
                if int(query.get("playlist-count") or 0) > prev_count:
                    loaded = True
                    break
            except (TypeError, ValueError):
                pass
            time.sleep(0.05)
        if not loaded:
            # Restore reality: the failed station never became current, so the
            # old marker (if any) is reinstated and the status re-derived from
            # mpv. No play-index, drop, history or success is reported.
            if old_marker:
                set_radio_current(old_marker)
            else:
                clear_radio_current()
            write_status_from_mpv(get_mpv_props() or {})
            print(json.dumps({"ok": False, "error": "Station failed to load"}))
            return
        mpv_send("playlist-play-index", ["0"])
        mpv_send("set_property", ["pause", False])
        # playlist-pos is now 0 (the new station), so the old station's stream
        # rows are removed while the new marker survives.
        drop_radio_streams(keep_current=True, clear_marker=False)
        props = wait_for_track_change(before.get("path") or "",
                                      before.get("playlist-pos"))
    else:
        set_radio_current(row)
        mpv_kill()
        ensure_private_runtime_dir()
        proc = subprocess.Popen(_radio_mpv_argv(row["url"]),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        json_dump(MPV_PID_PATH, mpv_pid_record(proc))
        wait_for_mpv()
        props = wait_for_metadata()
        # mpv kept alive is not proof of audio: a dead URL it accepted would
        # otherwise be reported as a successful play and recorded in history.
        if mpv_is_running():
            ok, progress_props = wait_for_stream_progress(row["url"])
            if not ok:
                mpv_kill()
                clear_radio_current()
                write_status({"ok": False, "playing": False})
                print(json.dumps({"ok": False,
                                  "error": "Stream did not start playing"}))
                return
            props = progress_props or props
    if not mpv_is_running():
        clear_radio_current()
        write_status({"ok": False, "playing": False})
        print(json.dumps({"ok": False, "error": "Stream failed to play"}))
        return
    write_status_from_mpv(props)
    record_radio_play(row)
    _radio_browser_report_click(row["id"])
    print(json.dumps({"ok": True, "id": row["id"], "name": row["name"],
                      "live": True}))


def cmd_station_history(args):
    """List or clear the local radio play history. Offline."""
    args, _refresh = _strip_refresh(args)
    if args and args[0] == "clear":
        json_dump(RADIO_HISTORY_PATH, [], mode=0o600)
        print(json.dumps({"ok": True, "cleared": True}))
        return
    if not args:
        print(json.dumps({"ok": True, "items": load_radio_history()}))
        return
    try:
        limit = max(1, min(RADIO_HISTORY_MAX, int(args[0])))
    except ValueError:
        fail("Usage: yt-music-ctl station-history [limit|clear]")
    print(json.dumps({"ok": True, "items": load_radio_history()[:limit]}))


def cmd_history(args):
    args, refresh = _strip_refresh(args)
    limit = 100
    if args:
        try:
            limit = max(1, min(300, int(args[0])))
        except ValueError:
            fail("Usage: yt-music-ctl history [limit]")
    key = [limit]
    ttl = METADATA_CACHE_TTL["history"]
    if not refresh:
        payload, fresh = cache_read("history", key, ttl)
        if fresh and payload is not None:
            print(json.dumps(_cache_served(payload)))
            return
        if payload is not None:
            # Stale but usable: serve it now, refresh in the background.
            print(json.dumps(_cache_served(payload, stale=True)))
            spawn_background_refresh("history", [limit])
            return
    ytm = _ytmusic_for_cache("history", key, ttl, force_auth=refresh)
    if ytm is None:
        return
    try:
        rows = ytm.get_history() or []
        items = [song_row(t) for t in rows[:limit] if t.get("videoId")]
        payload = {"ok": True, "items": items, "cached": False}
        cache_write("history", key, payload)
        print(json.dumps(payload))
    except Exception as e:
        if not _serve_stale("history", key, ttl):
            print(json.dumps({"ok": False, "error": str(e)}))


def cmd_last_played(args):
    if args and args[0] == "clear":
        json_dump(LAST_PLAYED_PATH, [], mode=0o600)
        print(json.dumps({"ok": True, "cleared": True}))
        return
    limit = 100
    if args:
        try:
            limit = max(1, min(LAST_PLAYED_MAX, int(args[0])))
        except ValueError:
            fail("Usage: yt-music-ctl last-played [limit|clear]")
    meta = load_track_meta()
    items = []
    for entry in load_last_played()[:limit]:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("videoId")
        if not valid_video_id(video_id):
            continue
        sidecar = meta.get(video_id) or {}
        items.append({
            "videoId": video_id,
            "title": str(entry.get("title") or sidecar.get("title") or ""),
            "artist": str(entry.get("artist") or sidecar.get("artist") or ""),
            "album": str(entry.get("album") or sidecar.get("album") or ""),
            "duration": entry.get("duration") or sidecar.get("duration") or 0,
            "playedAt": entry.get("playedAt") or 0,
        })
    print(json.dumps({"ok": True, "items": items}))


def _spawn_session_mpv(ids, index, position, pause=True):
    # A saved session holds resolved songs only; a radio marker left over from
    # a previous station would otherwise describe this session's status as live.
    clear_radio_current()
    ensure_daemon()
    ensure_private_runtime_dir()
    urls = [watch_url(v) for v in ids]
    proc = subprocess.Popen(
        ["mpv", "--no-video", "--really-quiet",
         f"--input-ipc-server={MPV_SOCKET}", "--keep-open=no", "--pause=yes"]
        + mpv_volume_args() + urls,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    json_dump(MPV_PID_PATH, mpv_pid_record(proc))
    wait_for_mpv()
    time.sleep(0.3)
    mpv_send("set_property", ["playlist-pos", index])
    # playlist-pos is asynchronous: wait until mpv really reports the target
    # entry as current, otherwise the seek below lands on the wrong track.
    target = ids[index]
    deadline = time.time() + 6
    while time.time() < deadline:
        if extract_video_id(get_mpv_props() or {}) == target:
            break
        time.sleep(0.15)
    if position > 1:
        mpv_send("seek", [position, "absolute"])
        time.sleep(0.2)
    mpv_send("set_property", ["pause", bool(pause)])
    props = wait_for_metadata()
    if isinstance(props, dict):
        props = dict(props)
        props["pause"] = bool(pause)
    write_status_from_mpv(props)


def _restore_session(pause=True):
    """Validate the saved session and spawn its queue when idle.

    Shared by restore/toggle/resume: all three must agree on what a resumable
    session is. Returns (restored, info); info carries a reason when it is not
    restored so each caller can phrase its own output.
    """
    if mpv_is_running():
        return False, {"reason": "already-playing"}
    session = load_session()
    ids = session.get("videoIds") if isinstance(session, dict) else None
    ids = [v for v in ids if valid_video_id(v)] if isinstance(ids, list) else []
    if not ids:
        return False, {"reason": "no-session"}
    try:
        index = max(0, min(len(ids) - 1, int(session.get("index") or 0)))
    except (TypeError, ValueError):
        index = 0
    try:
        position = max(0.0, float(session.get("position") or 0))
    except (TypeError, ValueError):
        position = 0.0
    _spawn_session_mpv(ids, index, position, pause=pause)
    return True, {"count": len(ids), "index": index}


def cmd_restore(args):
    """Rebuild the last queue paused at its saved index + position."""
    restored, info = _restore_session(pause=True)
    print(json.dumps({"ok": True, "restored": restored, **info}))


def cmd_lyrics(args):
    args, refresh = _strip_refresh(args)
    if len(args) != 1 or not valid_video_id(args[0]):
        fail("Usage: yt-music-ctl lyrics <videoId>")
    video_id = args[0]
    key = [video_id]
    ttl = METADATA_CACHE_TTL["lyrics"]
    if not refresh:
        payload, fresh = cache_read("lyrics", key, ttl)
        if fresh and payload is not None:
            print(json.dumps(_cache_served(payload)))
            return
        if payload is not None:
            # Stale but usable: serve it now, refresh in the background.
            print(json.dumps(_cache_served(payload, stale=True)))
            spawn_background_refresh("lyrics", [video_id])
            return
    ytm = _ytmusic_for_cache("lyrics", key, ttl, force_auth=refresh)
    if ytm is None:
        return
    try:
        watch = ytm.get_watch_playlist(video_id)
        lyrics_id = watch.get("lyrics") if isinstance(watch, dict) else None
        if not lyrics_id:
            # Plenty of tracks genuinely have no lyrics — that is not an error.
            # The resolved "no lyrics" answer is still cacheable.
            payload = {"ok": True, "videoId": video_id,
                       "hasLyrics": False, "lines": [], "source": None,
                       "cached": False}
            cache_write("lyrics", key, payload)
            print(json.dumps(payload))
            return
        data = ytm.get_lyrics(lyrics_id) or {}
        raw_lines = [line.strip() for line in str(data.get("lyrics") or "").split("\n")]
        while raw_lines and not raw_lines[0]:
            raw_lines.pop(0)
        while raw_lines and not raw_lines[-1]:
            raw_lines.pop()
        lines = []
        index = 0
        while index < len(raw_lines):
            if raw_lines[index]:
                lines.append(raw_lines[index])
                index += 1
                continue
            run = 0
            while index < len(raw_lines) and not raw_lines[index]:
                run += 1
                index += 1
            # Keep stanza gaps readable: collapse runaway blank runs to one.
            if run > 2:
                lines.append("")
            else:
                lines.extend([""] * run)
        # Synced lyrics are fetched separately because timestamps=True often
        # comes back as HTTP 400 even when plain lyrics resolve fine.
        synced = None
        try:
            sdata = ytm.get_lyrics(lyrics_id, timestamps=True)
            if sdata and sdata.get("hasTimestamps") and isinstance(sdata.get("lyrics"), list):
                synced = sdata["lyrics"]
        except Exception:
            synced = None
        if not isinstance(synced, list) or not synced:
            synced = None
        payload = {
            "ok": True,
            "videoId": video_id,
            "hasLyrics": True,
            "source": data.get("source") or None,
            "lines": lines,
            "synced": synced,
            "cached": False,
        }
        cache_write("lyrics", key, payload)
        print(json.dumps(payload))
    except Exception as e:
        # Only successful answers are cached, so anything stale here is good.
        if not _serve_stale("lyrics", key, ttl):
            print(json.dumps({"ok": False, "error": str(e)}))


def _image_host_allowed(host):
    host = (host or "").lower()
    return any(host == suffix or host.endswith("." + suffix)
               for suffix in IMAGE_HOST_SUFFIXES)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects so a 3xx cannot escape the image host allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urlopen_no_redirect(request, timeout):
    return urllib.request.build_opener(_NoRedirectHandler).open(request, timeout=timeout)


def _prune_media_cache(directory, max_entries, max_bytes):
    """Evict oldest files until both caps hold. Never raises.

    Dotfiles (in-progress mkstemp writes) are ignored so a concurrent fetch is
    not truncated. Non-regular files count toward the entry cap and are removed
    rather than trusted.
    """
    try:
        entries = []
        for name in os.listdir(directory):
            if name.startswith("."):
                continue
            path = os.path.join(directory, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            size = st.st_size if stat.S_ISREG(st.st_mode) else 0
            entries.append((st.st_mtime, size, path))
    except OSError:
        return
    entries.sort(key=lambda item: item[0])
    total = sum(size for _mtime, size, _path in entries)
    while entries and (len(entries) > max_entries or total > max_bytes):
        _mtime, size, path = entries.pop(0)
        total -= size
        try:
            os.unlink(path)
        except OSError:
            pass


class _ThumbnailError(Exception):
    """A fetched thumbnail that failed validation and must not be cached."""


def ensure_thumbnail(video_id, errors=None):
    """Return THUMBNAIL_CACHE_DIR/<video_id>.jpg, downloading it first.

    Never raises: an invalid id, a failed fetch, or a rejected image all come
    back as None. When `errors` (a list) is supplied, its first item is the
    reason for that None — the `thumbnail` CLI command passes one so it can
    keep reporting exactly why a fetch failed.
    """
    if not valid_video_id(video_id):
        if errors is not None:
            errors.append("Invalid video ID")
        return None
    path = os.path.join(THUMBNAIL_CACHE_DIR, f"{video_id}.jpg")
    try:
        _ensure_private_dir(CACHE_ROOT)
        _ensure_private_dir(THUMBNAIL_CACHE_DIR)
        try:
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode) and st.st_size > 0:
                return path  # already cached
        except OSError:
            pass
        request = urllib.request.Request(
            f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            headers={"User-Agent": "yt-music-ctl/1"})
        with _urlopen_no_redirect(request, timeout=8) as response:
            data = response.read(MAX_THUMBNAIL_BYTES + 1)
        if len(data) > MAX_THUMBNAIL_BYTES:
            raise _ThumbnailError("Thumbnail is too large")
        dimensions = jpeg_dimensions(data)
        if (not dimensions or dimensions[0] > MAX_THUMBNAIL_DIMENSION or
                dimensions[1] > MAX_THUMBNAIL_DIMENSION or
                dimensions[0] * dimensions[1] > MAX_THUMBNAIL_PIXELS):
            raise _ThumbnailError("Thumbnail dimensions are not allowed")
        fd, temporary = tempfile.mkstemp(dir=THUMBNAIL_CACHE_DIR, prefix=".thumb-", suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _prune_media_cache(THUMBNAIL_CACHE_DIR,
                           THUMBNAIL_CACHE_MAX_ENTRIES,
                           THUMBNAIL_CACHE_MAX_BYTES)
        return path
    except _ThumbnailError as exc:
        if errors is not None:
            errors.append(str(exc))
        return None
    except Exception as exc:
        if errors is not None:
            errors.append(f"Thumbnail fetch failed: {exc}")
        return None


def cmd_thumbnail(args):
    if not args or not valid_video_id(args[0]):
        fail("Invalid video ID")
    errors = []
    if ensure_thumbnail(args[0], errors) is None:
        fail(errors[0] if errors else "Thumbnail fetch failed")


def cmd_image(args):
    if not args or not args[0].startswith(("http://", "https://")):
        fail("Usage: yt-music-ctl image <url>")
    url = args[0]
    host = (urllib.parse.urlsplit(url).hostname or "")
    if not _image_host_allowed(host):
        fail("Image host not allowed")
    _ensure_private_dir(CACHE_ROOT)
    _ensure_private_dir(IMAGE_CACHE_DIR)
    key = hashlib.sha256(url.encode()).hexdigest()[:32]
    path = os.path.join(IMAGE_CACHE_DIR, f"{key}.jpg")
    try:
        if _owned_regular_file(path) and os.path.getsize(path) > 0:
            return
        request = urllib.request.Request(url, headers={"User-Agent": "yt-music-ctl/1"})
        with _urlopen_no_redirect(request, timeout=8) as response:
            if not _image_host_allowed(
                    urllib.parse.urlsplit(response.geturl()).hostname or ""):
                fail("Image host not allowed")
            data = response.read(MAX_THUMBNAIL_BYTES + 1)
        if len(data) > MAX_THUMBNAIL_BYTES:
            fail("Image is too large")
        if data[:2] == b"\xff\xd8":
            dimensions = jpeg_dimensions(data)
            if (not dimensions or dimensions[0] > MAX_THUMBNAIL_DIMENSION or
                    dimensions[1] > MAX_THUMBNAIL_DIMENSION or
                    dimensions[0] * dimensions[1] > MAX_THUMBNAIL_PIXELS):
                fail("Image dimensions are not allowed")
        elif data[:4] != b"\x89PNG":
            fail("Image is not a JPEG or PNG")
        fd, temporary = tempfile.mkstemp(dir=IMAGE_CACHE_DIR, prefix=".img-", suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _prune_media_cache(IMAGE_CACHE_DIR, IMAGE_CACHE_MAX_ENTRIES, IMAGE_CACHE_MAX_BYTES)
    except Exception as exc:
        fail(f"Image fetch failed: {exc}")


# ------------------------------------------------------------- precache audio

def audio_cache_path(video_id):
    """Existing cached audio file for video_id, or "" when there is none.

    Only regular files whose stem is exactly `<videoId>.` are considered, so
    neither a planted symlink nor a lookalike id can be returned.
    """
    if not valid_video_id(video_id):
        return ""
    try:
        names = os.listdir(AUDIO_CACHE_DIR)
    except OSError:
        return ""
    for name in names:
        if not name.startswith(video_id + "."):
            continue
        path = os.path.join(AUDIO_CACHE_DIR, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            return path
    return ""


def cache_audio_video_id(path):
    """videoId for a local precached audio path, else "".

    mpv reports `path`/`filename` verbatim, so a track played from the cache
    is identified by its file name instead of a watch URL.
    """
    if not isinstance(path, str) or not path.startswith(AUDIO_CACHE_DIR + os.sep):
        return ""
    stem = os.path.basename(path).rsplit(".", 1)[0]
    return stem if valid_video_id(stem) else ""


def _audio_cache_entries():
    """(mtime, size, path) for each regular cache file, oldest first.

    Symlinks and other non-regular files are skipped (lstat only), so this
    never follows a planted link — same rule as _cache_entries.
    """
    entries = []
    try:
        names = os.listdir(AUDIO_CACHE_DIR)
    except OSError:
        return entries
    for name in names:
        path = os.path.join(AUDIO_CACHE_DIR, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        entries.append((st.st_mtime, st.st_size, path))
    entries.sort(key=lambda item: item[0])
    return entries


def _cleanup_stale_downloads(grace=AUDIO_DOWNLOAD_GRACE):
    """Remove leftover `.dl-*` temp dirs from interrupted downloads.

    A normally-failing yt-dlp run is cleaned up in precache_track's finally
    block, but SIGTERM (e.g. the daemon being stopped mid-download) skips it
    and would otherwise leak a multi-megabyte file that audio_cache_prune
    never sees. Only dirs older than `grace` seconds are removed so a live
    download started by another process is never destroyed. Never raises.
    """
    cutoff = time.time() - max(0.0, float(grace))
    try:
        names = os.listdir(AUDIO_CACHE_DIR)
    except OSError:
        return
    for name in names:
        if not name.startswith(".dl-"):
            continue
        path = os.path.join(AUDIO_CACHE_DIR, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            continue
        if st.st_mtime > cutoff:
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass


def audio_cache_prune(max_bytes=None):
    """Delete oldest files until the cache fits the byte cap. Never raises."""
    cap = AUDIO_CACHE_MAX_BYTES if max_bytes is None else int(max_bytes)
    try:
        _cleanup_stale_downloads()
        entries = _audio_cache_entries()
        total = sum(size for _mtime, size, _path in entries)
        while entries and total > cap:
            _mtime, size, path = entries.pop(0)
            total -= size
            try:
                st = os.lstat(path)
                if stat.S_ISREG(st.st_mode):
                    os.unlink(path)
            except OSError:
                pass
    except Exception:
        pass


def _download_output(tmp_dir):
    """Largest regular file yt-dlp wrote into tmp_dir, or ""."""
    candidates = []
    try:
        names = os.listdir(tmp_dir)
    except OSError:
        return ""
    for name in names:
        if name.startswith(".") or name.endswith((".part", ".ytdl")):
            continue
        path = os.path.join(tmp_dir, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            candidates.append((st.st_size, path))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def _ytdlp_error(proc):
    """Short, single-line reason from a failed yt-dlp run."""
    text = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    line = text[-1] if text else ""
    if len(line) > 160:
        line = line[:157] + "..."
    return line or f"yt-dlp exited {proc.returncode}"


def precache_track(video_id, force=False):
    """Download one track's audio into AUDIO_CACHE_DIR. Never raises.

    Returns {"ok", "error", "cached", "path"}: cached is True when the file
    was already present (and force was not set), path is "" on failure.

    yt-dlp runs as the system binary with the minimal flag set:

        -f bestaudio/best --no-playlist --no-progress --embed-metadata -o <tmpl> <url>

    There is deliberately no `-x`/`--audio-format`: bestaudio for YouTube is a
    single already-compressed stream (opus-in-webm or m4a), which mpv plays
    natively through the same container it would have streamed. Skipping the
    extract/convert step removes the transcode and a whole class of
    partial-output failures, at the cost of a `.webm`/`.m4a` extension
    instead of a fixed one (the file is renamed to `<videoId>.<ext>` either
    way). `--embed-metadata` muxes title/artist/album tags into that container
    in place (same extension, yt-dlp uses the system ffmpeg for the copy), so
    an MPRIS client shows a real track instead of a bare file name. Output
    goes to a private temp directory inside the cache so a failed
    or interrupted run can never leave a partial file behind.
    """
    if not valid_video_id(video_id):
        return {"ok": False, "error": "Invalid video ID", "cached": False, "path": ""}
    tmp_dir = None
    try:
        _ensure_private_dir(CACHE_ROOT)
        _ensure_private_dir(AUDIO_CACHE_DIR)
        # Reap temp dirs from downloads that died without running their
        # finally block (e.g. the daemon was SIGTERM'd mid-download). Done
        # before the cache-hit early return so a repeat call still cleans up.
        _cleanup_stale_downloads()
        existing = audio_cache_path(video_id)
        if existing and not force:
            try:
                os.utime(existing, None)  # LRU touch: used is recent
            except OSError:
                pass
            return {"ok": True, "error": "", "cached": True, "path": existing}
        tmp_dir = tempfile.mkdtemp(dir=AUDIO_CACHE_DIR, prefix=".dl-")
        template = os.path.join(tmp_dir, "%(id)s.%(ext)s")
        url = f"https://music.youtube.com/watch?v={video_id}"
        proc = subprocess.run(
            [YTDLP_BIN, "-f", "bestaudio/best", "--no-playlist",
             "--no-progress", "--embed-metadata", "-o", template, url],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=AUDIO_PRECACHE_TIMEOUT)
        if proc.returncode != 0:
            return {"ok": False, "error": _ytdlp_error(proc),
                    "cached": False, "path": ""}
        produced = _download_output(tmp_dir)
        if not produced:
            return {"ok": False, "error": "yt-dlp produced no file",
                    "cached": False, "path": ""}
        if os.path.basename(produced).rsplit(".", 1)[0] != video_id:
            return {"ok": False, "error": "yt-dlp returned a different video",
                    "cached": False, "path": ""}
        try:
            st = os.lstat(produced)
        except OSError:
            st = None
        if st is None or not stat.S_ISREG(st.st_mode):
            return {"ok": False, "error": "yt-dlp produced no file",
                    "cached": False, "path": ""}
        if st.st_size <= 0:
            return {"ok": False, "error": "Downloaded file is empty",
                    "cached": False, "path": ""}
        if st.st_size > AUDIO_CACHE_MAX_FILE_BYTES:
            return {"ok": False, "error": "Audio file is too large",
                    "cached": False, "path": ""}
        extension = os.path.splitext(produced)[1]
        if not extension or len(extension) > 8:
            return {"ok": False, "error": "Unexpected output name",
                    "cached": False, "path": ""}
        target = os.path.join(AUDIO_CACHE_DIR, video_id + extension)
        os.chmod(produced, 0o600)
        os.replace(produced, target)
        audio_cache_prune()
        # Best-effort cover art for the local file: MPRIS reads
        # `mpris:artUrl` from this thumbnail once the entry plays. It never
        # raises and never fails the precache.
        ensure_thumbnail(video_id)
        return {"ok": True, "error": "", "cached": False, "path": target}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Download timed out", "cached": False, "path": ""}
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        if len(message) > 160:
            message = message[:157] + "..."
        return {"ok": False, "error": message, "cached": False, "path": ""}
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _playlist_state():
    """(playlist, playlist-pos) from mpv, or (None, -1) when unreadable."""
    props = mpv_query(["playlist", "playlist-pos"])
    if not isinstance(props, dict):
        return None, -1
    playlist = props.get("playlist")
    playlist = playlist if isinstance(playlist, list) else None
    try:
        pos = int(props.get("playlist-pos"))
    except (TypeError, ValueError):
        pos = -1
    return playlist, pos


def _is_stream_entry(entry):
    """True when a playlist entry is a stream.

    A stream is an http(s) entry video_id_from_url cannot parse at all (a
    non-YouTube host). A YouTube URL is never a stream: an empty/invalid `v`
    makes video_id_from_url return None, which is the same as a foreign host,
    so the host check keeps the two apart.
    """
    if not isinstance(entry, dict):
        return False
    filename = entry.get("filename") or ""
    if video_id_from_url(filename) is not None or _is_youtube_url(filename):
        return False
    return filename.startswith(("http://", "https://"))


def drop_radio_streams(keep_current=True, clear_marker=True):
    """Remove queued stream rows, keeping the one currently playing by default.

    `keep_current` keeps the stream entry at mpv's current `playlist-pos`; pass
    False to drop every stream. `clear_marker` drops the live radio marker
    afterwards. Ageing them out is the norm once another track is selected, so
    they leave the queue; removal goes in descending index order so earlier
    removals do not shift later ones. Station-play passes clear_marker=False
    because it has just stamped the new marker and wants it to survive the
    cleanup. Never raises.
    """
    try:
        if mpv_is_running():
            props = mpv_query(["playlist", "playlist-pos"])
            playlist = props.get("playlist") if isinstance(props, dict) else None
            if isinstance(playlist, list):
                try:
                    pos = int(props.get("playlist-pos"))
                except (TypeError, ValueError):
                    pos = -1
                if keep_current:
                    # Only a readable, in-range position names the stream to
                    # keep. An unknown position (-1/out of range) means we
                    # cannot tell which row is current, so drop nothing rather
                    # than risk removing every stream including the playing one.
                    if 0 <= pos < len(playlist):
                        drop = [index for index, entry
                                in enumerate(playlist)
                                if index != pos and _is_stream_entry(entry)]
                    else:
                        drop = []
                else:
                    drop = [index for index, entry in enumerate(playlist)
                            if _is_stream_entry(entry)]
                for index in sorted(drop, reverse=True):
                    mpv_send("playlist-remove", [index])
    except Exception:
        pass
    if clear_marker:
        clear_radio_current()


def prune_radio_if_moved_on():
    """Drop a lingering station row once a song is playing.

    A station row is current only while the stream entry plays; after a
    jump/next/prev lands on a song the station is stale and leaves the queue.
    Never raises.
    """
    try:
        if not load_radio_current():
            return
        # Inspect the entry at the current position, not mpv's `path`: right
        # after a jump `playlist-pos` is already the song while `path` still
        # names the stream, so a path-based check would wrongly skip the drop.
        props = mpv_query(["playlist", "playlist-pos"])
        playlist = props.get("playlist") if isinstance(props, dict) else None
        if not isinstance(playlist, list):
            return
        try:
            pos = int(props.get("playlist-pos"))
        except (TypeError, ValueError):
            return
        if 0 <= pos < len(playlist) and not _is_stream_entry(playlist[pos]):
            drop_radio_streams()
    except Exception:
        pass


def next_queue_video_id():
    """videoId of the queue entry after the current one, or ""."""
    try:
        playlist, pos = _playlist_state()
        if not playlist or pos < 0 or pos + 1 >= len(playlist):
            return ""
        entry = playlist[pos + 1]
        video_id = (video_id_from_url(entry.get("filename") or "")
                    if isinstance(entry, dict) else None)
        return video_id if valid_video_id(video_id) else ""
    except Exception:
        return ""


def _remove_playlist_path(path):
    """Drop the playlist entry that plays `path`, if there is one."""
    try:
        playlist, _pos = _playlist_state()
        if not playlist:
            return False
        for index, entry in enumerate(playlist):
            if isinstance(entry, dict) and entry.get("filename") == path:
                mpv_send("playlist-remove", index)
                return True
    except Exception:
        pass
    return False


def install_cached_next(video_id, path):
    """Make the queue's next entry play the local cached file.

    mpv's playlist holds streaming URLs, so "play from cache" means swapping
    that entry in place: `loadfile <path> insert-next` puts the local file at
    pos+1 and pushes the original next entry down to pos+2, and that displaced
    duplicate is removed only after its videoId is confirmed to be the one we
    replaced. The queue keeps its length and its order; on any mismatch nothing
    is removed (or the just-inserted entry is rolled back). The swap is also
    skipped when the track sidecar has no title for the id (mpv would otherwise
    expose a bare file name to the UI), which leaves the entry streaming exactly
    as before. Never raises.

    Returns {"ok": bool, "error": str, "installed": bool}.
    """
    if not valid_video_id(video_id) or not path:
        return {"ok": False, "error": "Nothing to install", "installed": False}
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return {"ok": False, "error": "Cache file missing", "installed": False}
    except OSError:
        return {"ok": False, "error": "Cache file missing", "installed": False}
    if not mpv_is_running():
        return {"ok": False, "error": "Nothing playing", "installed": False}
    try:
        playlist, pos = _playlist_state()
        if not playlist or pos < 0 or pos + 1 >= len(playlist):
            return {"ok": False, "error": "No next track", "installed": False}
        nxt = pos + 1
        entry = playlist[nxt] if isinstance(playlist[nxt], dict) else {}
        filename = entry.get("filename") or ""
        if filename == path:
            return {"ok": True, "error": "", "installed": False}
        if video_id_from_url(filename) != video_id:
            return {"ok": False, "error": "Next queue entry changed",
                    "installed": False}
        # Swap the file in only when the sidecar can still name the track:
        # the audio stream carries no tags, so without a stored title the bar
        # and queue-list would show a bare file name — worse than streaming.
        # The audio stays precached either way.
        sidecar = load_track_meta().get(video_id)
        if not (isinstance(sidecar, dict) and str(sidecar.get("title") or "").strip()):
            return {"ok": False, "error": "No stored metadata for this track",
                    "installed": False}
        # Hand mpv the cover art as a file-local option when the thumbnail is
        # already cached: a bare local file has no YouTube URL for mpv-mpris to
        # derive `mpris:artUrl` from, and the sidecar image is what the media
        # widget shows. Without a thumbnail the plain two-argument insert is
        # used and everything else behaves exactly as before.
        thumb = os.path.join(THUMBNAIL_CACHE_DIR, f"{video_id}.jpg")
        try:
            thumb_stat = os.lstat(thumb)
            has_thumb = stat.S_ISREG(thumb_stat.st_mode) and thumb_stat.st_size > 0
        except OSError:
            has_thumb = False
        if has_thumb:
            resp = mpv_send("loadfile",
                            [path, "insert-next", -1, {"cover-art-files": thumb}])
        else:
            resp = mpv_send("loadfile", [path, "insert-next"])
        if not isinstance(resp, dict) or resp.get("error") != "success":
            return {"ok": False, "error": "mpv refused the cached file",
                    "installed": False}
        after, after_pos = _playlist_state()
        if (not after or len(after) != len(playlist) + 1 or after_pos != pos
                or not isinstance(after[nxt], dict)
                or after[nxt].get("filename") != path):
            _remove_playlist_path(path)
            return {"ok": False, "error": "Queue changed during install",
                    "installed": False}
        displaced = after[nxt + 1] if nxt + 1 < len(after) else {}
        displaced_id = (video_id_from_url(displaced.get("filename") or "")
                        if isinstance(displaced, dict) else None)
        if displaced_id != video_id:
            # Not the entry we meant to replace: undo the insert instead of
            # deleting somebody else's queue entry.
            mpv_send("playlist-remove", nxt)
            return {"ok": False, "error": "Queue changed during install",
                    "installed": False}
        mpv_send("playlist-remove", nxt + 1)
        final, final_pos = _playlist_state()
        if (not final or len(final) != len(playlist) or final_pos != pos
                or not isinstance(final[nxt], dict)
                or final[nxt].get("filename") != path):
            return {"ok": False, "error": "Queue changed during install",
                    "installed": False}
        try:
            os.utime(path, None)  # keep the queued file out of the first evictions
        except OSError:
            pass
        return {"ok": True, "error": "", "installed": True}
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        if len(message) > 160:
            message = message[:157] + "..."
        return {"ok": False, "error": message, "installed": False}


def precache_next(force=False):
    """Precache the queue entry after the current one. Never raises.

    Returns {"ok", "videoId", "error", "cached", "path"}; videoId is "" when
    there is no identifiable next entry.
    """
    try:
        video_id = next_queue_video_id()
        if not video_id:
            return {"ok": False, "videoId": "", "error": "No next track",
                    "cached": False, "path": ""}
        result = precache_track(video_id, force=force)
        return {"ok": bool(result.get("ok")), "videoId": video_id,
                "error": str(result.get("error") or ""),
                "cached": bool(result.get("cached")),
                "path": str(result.get("path") or "")}
    except Exception as exc:
        return {"ok": False, "videoId": "", "error": str(exc),
                "cached": False, "path": ""}


# One background download at a time, plus two remembered ids:
#   requested - last target handed to a worker (never downloaded twice in a row)
#   for_track - the currently playing id whose "next" entry has been handled
# so the status loop can ask for a precache on every flush without queueing
# work up or re-querying mpv in a tight loop.
_PRECACHE_LOCK = threading.Lock()
_PRECACHE_STATE = {"requested": "", "for_track": ""}


def schedule_precache(current_id):
    """Precache (and install) the entry after `current_id` on a worker thread.

    Returns True when a worker thread was started. At most one worker runs at
    a time — while a download is in flight the lock is held and this returns
    immediately without touching mpv, so the caller can simply ask again on its
    next flush. Every exception inside the worker is swallowed and nothing is
    logged.
    """
    if not valid_video_id(current_id):
        return False
    if _PRECACHE_STATE["for_track"] == current_id:
        return False  # this playing track was already handled
    if not _PRECACHE_LOCK.acquire(blocking=False):
        return False  # a download is already running; try again on next flush
    started = False
    try:
        target = next_queue_video_id()
        if not target:
            # The playlist may simply not be readable yet (mpv still starting,
            # or enqueue's fresh mpv not attached). Do NOT latch for_track, or
            # this track would be marked handled and never precached.
            return False
        if target == _PRECACHE_STATE["requested"]:
            # A worker for this exact target ran (or is running); nothing new.
            _PRECACHE_STATE["for_track"] = current_id
            return False

        def worker():
            try:
                result = precache_track(target)
                if result.get("ok"):
                    install_cached_next(target, str(result.get("path") or ""))
            except Exception:
                pass
            finally:
                _PRECACHE_LOCK.release()

        threading.Thread(target=worker, daemon=True, name="yt-music-precache").start()
        _PRECACHE_STATE["requested"] = target
        _PRECACHE_STATE["for_track"] = current_id
        started = True
        return True
    except Exception:
        return False
    finally:
        if not started:
            try:
                _PRECACHE_LOCK.release()
            except RuntimeError:
                pass


def cmd_precache(args):
    if args:
        video_id = args[0]
        if not valid_video_id(video_id):
            print(json.dumps({"ok": False, "error": "Invalid video ID"}))
            return
        result = precache_track(video_id)
        if result.get("ok"):
            print(json.dumps({"ok": True, "videoId": video_id,
                              "cached": bool(result.get("cached")),
                              "path": str(result.get("path") or "")}))
        else:
            print(json.dumps({"ok": False,
                              "error": str(result.get("error") or "Download failed")}))
        return
    if not mpv_is_running():
        print(json.dumps({"ok": False, "error": "Nothing playing"}))
        return
    result = precache_next()
    if not result.get("ok"):
        print(json.dumps({"ok": False,
                          "error": str(result.get("error") or "Nothing to precache")}))
        return
    # Cache first, then point the queue's next entry at the local file.
    install_cached_next(str(result.get("videoId") or ""), str(result.get("path") or ""))
    print(json.dumps({"ok": True, "videoId": result.get("videoId"),
                      "cached": bool(result.get("cached")),
                      "path": str(result.get("path") or "")}))


def _mix_launch(track_list):
    """Replace the current mpv playlist with the mix and refresh status.

    This is the playback side effect of cmd_mix: it must run on every path
    that serves a mix, cache hit included.
    """
    remember_tracks(track_list)
    clear_radio_current()
    mpv_kill()
    clear_session()
    urls = [watch_url(t['videoId']) for t in track_list]
    ensure_private_runtime_dir()
    proc = subprocess.Popen(["mpv", "--no-video", "--really-quiet",
                             f"--input-ipc-server={MPV_SOCKET}",
                             "--keep-open=no"] + mpv_volume_args() + urls,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    json_dump(MPV_PID_PATH, mpv_pid_record(proc))
    wait_for_mpv()
    props = wait_for_metadata()
    write_status_from_mpv(props, spawn_precache=True)


def _mix_output(payload):
    """The printed JSON for a mix, rebuilt from a cached metadata record."""
    return {"ok": True, "mix": True, "seedId": payload.get("seedId", ""),
            "tracks": len(payload.get("trackList") or [])}


def cmd_mix(args):
    args, refresh = _strip_refresh(args)
    ensure_daemon()
    if not args:
        fail("Usage: yt-music-ctl mix <videoId> [playlistId]")
    seed_id = args[0]
    key = list(args)
    ttl = METADATA_CACHE_TTL["mix"]
    if not refresh:
        payload, fresh = cache_read("mix", key, ttl)
        track_list = payload.get("trackList") if isinstance(payload, dict) else None
        if fresh and isinstance(track_list, list) and track_list:
            # Metadata comes from the cache, playback still happens here.
            _mix_launch(track_list)
            print(json.dumps(_cache_served(_mix_output(payload))))
            return
        if isinstance(track_list, list) and track_list:
            # Stale but usable: start playback now, refresh in the background.
            print(json.dumps(_cache_served(_mix_output(payload), stale=True)))
            spawn_background_refresh("mix", list(args))
            _mix_launch(track_list)
            return
    ytm = get_ytmusic(require_auth=False, force_auth=refresh)
    try:
        watchlist = ytm.get_watch_playlist(seed_id, limit=50)
        tracks = []
        for track in (watchlist.get("tracks") or []):
            vid = track.get("videoId", "")
            if not vid:
                continue
            album = track.get("album") or {}
            if not isinstance(album, dict):
                album = {"name": album}
            tracks.append({
                "videoId": vid,
                "title": track.get("title", ""),
                "artist": ", ".join(a.get("name", "") for a in (track.get("artists") or [])),
                "album": album.get("title", "") or album.get("name", ""),
                "duration": track.get("duration_seconds", 0) or 0,
            })
        if not tracks:
            print(json.dumps({"ok": False, "error": "No mix tracks found"}))
            return
        _mix_launch(tracks)
        payload = {"seedId": seed_id, "trackList": tracks}
        cache_write("mix", key, payload)
        print(json.dumps({**_mix_output(payload), "cached": False}))
    except Exception as e:
        stale, _fresh = cache_read("mix", key, ttl)
        stale_tracks = stale.get("trackList") if isinstance(stale, dict) else None
        if isinstance(stale_tracks, list) and stale_tracks:
            try:
                _mix_launch(stale_tracks)
            except Exception:
                pass
            print(json.dumps(_cache_served(_mix_output(stale), stale=True)))
        else:
            print(json.dumps({"ok": False, "error": str(e)}))


def cmd_queue_playlist(args):
    ensure_daemon()
    if not args:
        fail("Usage: yt-music-ctl queue <playlistId>")
    playlist_id = args[0]
    ytm = get_ytmusic()
    try:
        pl = ytm.get_playlist(playlist_id, limit=None)
        tracks = pl.get("tracks") or []
        urls = []
        meta = []
        for t in tracks:
            vid = t.get("videoId", "")
            if vid:
                urls.append(watch_url(vid))
                album = t.get("album") or {}
                if not isinstance(album, dict):
                    album = {"name": album}
                meta.append({
                    "videoId": vid,
                    "title": t.get("title", ""),
                    "artist": ", ".join(a.get("name", "")
                                        for a in (t.get("artists") or [])),
                    "album": album.get("title", "") or album.get("name", ""),
                    "duration": t.get("duration_seconds", 0) or 0,
                })
        if not urls:
            print(json.dumps({"ok": False, "error": "Empty playlist"}))
            return
        remember_tracks(meta)
        mpv_kill()
        clear_radio_current()
        clear_session()
        ensure_private_runtime_dir()
        proc = subprocess.Popen(["mpv", "--no-video", "--really-quiet",
                                 f"--input-ipc-server={MPV_SOCKET}",
                                 "--keep-open=no"] + mpv_volume_args() + urls,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        json_dump(MPV_PID_PATH, mpv_pid_record(proc))
        wait_for_mpv()
        props = wait_for_metadata()
        write_status_from_mpv(props, spawn_precache=True)
        print(json.dumps({
            "ok": True,
            "queued": True,
            "title": pl.get("title", ""),
            "tracks": len(urls)
        }))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_enqueue(args):
    ensure_daemon()
    usage = "Usage: yt-music-ctl enqueue <play|queue|next> <album|artist|playlist> <id>"
    if len(args) != 3:
        fail(usage)
    mode, kind, target_id = args
    if mode not in ("play", "queue", "next"):
        fail(usage)
    if kind not in ("album", "artist", "playlist"):
        fail(usage)
    if not target_id:
        fail(usage)
    ytm = get_ytmusic()
    try:
        if kind == "album":
            data = ytm.get_album(target_id)
            tracks = data.get("tracks") or []
        elif kind == "artist":
            data = ytm.get_artist(target_id)
            tracks = ((data.get("songs") or {}).get("results") or [])
        else:  # playlist
            data = ytm.get_playlist(target_id, limit=None)
            tracks = data.get("tracks") or []
        meta = []
        for t in tracks:
            vid = t.get("videoId", "")
            if not vid:
                continue
            album = t.get("album") or {}
            if not isinstance(album, dict):
                album = {"name": album}
            meta.append({
                "videoId": vid,
                "title": t.get("title", ""),
                "artist": ", ".join(a.get("name", "")
                                    for a in (t.get("artists") or [])),
                "album": album.get("title", "") or album.get("name", ""),
                "duration": t.get("duration_seconds", 0) or 0,
            })
        if not meta:
            print(json.dumps({"ok": False, "error": "No playable tracks"}))
            return
        remember_tracks(meta)
        urls = [watch_url(t['videoId']) for t in meta]
        if mode == "play" or not mpv_is_running():
            mpv_kill()
            clear_radio_current()
            clear_session()
            ensure_private_runtime_dir()
            proc = subprocess.Popen(["mpv", "--no-video", "--really-quiet",
                                     f"--input-ipc-server={MPV_SOCKET}",
                                     "--keep-open=no"] + mpv_volume_args() + urls,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            json_dump(MPV_PID_PATH, mpv_pid_record(proc))
            wait_for_mpv()
            props = wait_for_metadata()
            write_status_from_mpv(props, spawn_precache=True)
            print(json.dumps({
                "ok": True,
                "played": True,
                "mode": mode,
                "kind": kind,
                "id": target_id,
                "tracks": len(meta)
            }))
        elif mode == "queue":
            for url in urls:
                mpv_send("loadfile", [url, "append"])
            print(json.dumps({
                "ok": True,
                "queued": True,
                "mode": mode,
                "kind": kind,
                "id": target_id,
                "tracks": len(meta)
            }))
        else:  # next: reverse so the original order survives insert-next
            for url in reversed(urls):
                mpv_send("loadfile", [url, "insert-next"])
            print(json.dumps({
                "ok": True,
                "queuedNext": True,
                "mode": mode,
                "kind": kind,
                "id": target_id,
                "tracks": len(meta)
            }))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_enqueue_files(args):
    ensure_daemon()
    usage = "Usage: yt-music-ctl enqueue-files <play|queue|next> <videoId...>"
    if len(args) < 2:
        fail(usage)
    mode = args[0]
    ids = args[1:]
    if mode not in ("play", "queue", "next"):
        fail(usage)
    for vid in ids:
        if not valid_video_id(vid):
            fail(usage)
    try:
        known = load_track_meta()
        meta = []
        for vid in ids:
            info = known.get(vid)
            info = info if isinstance(info, dict) else {}
            entry = {"videoId": vid}
            for key in ("title", "artist", "duration"):
                if info.get(key):
                    entry[key] = info[key]
            meta.append(entry)
        if not meta:
            print(json.dumps({"ok": False, "error": "No playable tracks"}))
            return
        remember_tracks(meta)
        urls = [watch_url(t['videoId']) for t in meta]
        if mode == "play" or not mpv_is_running():
            mpv_kill()
            clear_radio_current()
            clear_session()
            ensure_private_runtime_dir()
            proc = subprocess.Popen(["mpv", "--no-video", "--really-quiet",
                                     f"--input-ipc-server={MPV_SOCKET}",
                                     "--keep-open=no"] + mpv_volume_args() + urls,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            json_dump(MPV_PID_PATH, mpv_pid_record(proc))
            wait_for_mpv()
            props = wait_for_metadata()
            write_status_from_mpv(props, spawn_precache=True)
            print(json.dumps({
                "ok": True,
                "played": True,
                "mode": mode,
                "tracks": len(meta)
            }))
        elif mode == "queue":
            for url in urls:
                mpv_send("loadfile", [url, "append"])
            print(json.dumps({
                "ok": True,
                "queued": True,
                "mode": mode,
                "tracks": len(meta)
            }))
        else:  # next: reverse so the original order survives insert-next
            for url in reversed(urls):
                mpv_send("loadfile", [url, "insert-next"])
            print(json.dumps({
                "ok": True,
                "queuedNext": True,
                "mode": mode,
                "tracks": len(meta)
            }))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def _queue_count():
    """Return the mpv playlist count, or None when it cannot be read."""
    props = mpv_query(["playlist-count"])
    if props is None:
        return None
    try:
        return int(props.get("playlist-count"))
    except (TypeError, ValueError):
        return None


def _queue_index(args, usage):
    if not args:
        fail(usage)
    try:
        return int(args[0])
    except ValueError:
        fail("Index must be an integer")


def build_queue_rows(playlist, meta, radio=None):
    """Normalize an mpv playlist into queue rows.

    A row with an http(s) filename and no resolvable video id is a live
    stream: it is marked `stream`, pinned at the top of the queue, and labelled
    with the station name. `number` is the visible song ordinal; stream rows
    carry 0 so they show a LIVE badge instead.
    """
    radio = radio if isinstance(radio, dict) else {}
    station_name = str(radio.get("name") or "Radio")
    station_url = str(radio.get("url") or "")
    rows = []
    number = 0
    for index, entry in enumerate(playlist or []):
        if not isinstance(entry, dict):
            continue
        filename = entry.get("filename") or ""
        raw_video_id = video_id_from_url(filename)
        video_id = raw_video_id if valid_video_id(raw_video_id) else ""
        info = meta.get(video_id) if video_id else None
        info = info if isinstance(info, dict) else {}
        # A stream is an http(s) filename video_id_from_url cannot parse; a
        # YouTube URL with an empty/invalid `v` resolves to None too, so the
        # host check keeps it a song and never a live station.
        is_stream = (raw_video_id is None
                     and not _is_youtube_url(filename)
                     and filename.startswith(("http://", "https://")))
        if not is_stream:
            number += 1
        # Only the station actually playing is named; any other stream row is
        # the generic "Radio" so a stale row cannot borrow the live marker name.
        if is_stream and filename == station_url:
            title = station_name
        elif is_stream:
            title = "Radio"
        else:
            title = str(info.get("title") or entry.get("title") or "")
        rows.append({
            "index": index,
            "videoId": video_id,
            "title": title,
            "artist": str(info.get("artist") or ""),
            "album": str(info.get("album") or ""),
            "duration": info.get("duration") or 0,
            "current": bool(entry.get("current")),
            "stream": bool(is_stream),
            "number": number if not is_stream else 0,
        })
    return rows


def cmd_queue_list(args):
    empty = {"ok": True, "playing": False, "saved": False, "position": -1,
             "count": 0, "tracks": []}
    if not mpv_is_running():
        ids, index, _position = _session_ids()
        if not ids:
            print(json.dumps(empty))
            return
        tracks = _session_tracks(ids, index)
        print(json.dumps({"ok": True, "playing": False, "saved": True,
                          "position": index, "count": len(tracks),
                          "tracks": tracks}))
        return
    props = mpv_query(["playlist", "playlist-pos", "playlist-count"])
    if props is None:
        print(json.dumps(empty))
        return
    playlist = props.get("playlist")
    if not isinstance(playlist, list):
        playlist = []
    try:
        position = int(props.get("playlist-pos"))
    except (TypeError, ValueError):
        position = -1
    meta = load_track_meta()
    tracks = build_queue_rows(playlist, meta, radio=load_radio_current())
    print(json.dumps({"ok": True, "playing": True, "saved": False,
                      "position": position,
                      "count": len(tracks), "tracks": tracks}))


def cmd_queue_jump(args):
    if not mpv_is_running():
        if not args:
            fail("Usage: yt-music-ctl queue-jump <index>")
        try:
            index = int(args[0])
        except (TypeError, ValueError):
            fail("Usage: yt-music-ctl queue-jump <index>")
        ids, _cur, _pos = _session_ids()
        if not ids:
            print(json.dumps({"ok": False, "error": "No queue"}))
            return
        if not 0 <= index < len(ids):
            print(json.dumps({"ok": False, "error": "Index out of range"}))
            return
        _spawn_session_mpv(ids, index, 0.0, pause=False)
        print(json.dumps({"ok": True, "position": index}))
        return
    index = _queue_index(args, "Usage: yt-music-ctl queue-jump <index>")
    count = _queue_count()
    if count is None:
        fail("Unable to read playlist")
    # An out-of-range playlist-pos makes mpv exit, so validate first.
    if not 0 <= index < count:
        print(json.dumps({"ok": False, "error": "Index out of range"}))
        return
    before = get_mpv_props()
    mpv_send("set_property", ["playlist-pos", index])
    # Jumping selects the track, but a paused player would stay paused. "Jump
    # to track" means play it, so unpause after the new entry is current.
    if before:
        props = wait_for_track_change(before.get("path"), before.get("playlist-pos"))
    else:
        time.sleep(0.3)
        props = get_mpv_props()
    mpv_send("set_property", ["pause", False])
    if isinstance(props, dict):
        props = dict(props)
        props["pause"] = False
    write_status_from_mpv(props)
    prune_radio_if_moved_on()
    print(json.dumps({"ok": True, "position": index}))


def cmd_queue_remove(args):
    if not mpv_is_running():
        index = _queue_index(args, "Usage: yt-music-ctl queue-remove <index>")
        ids, _cur, _pos = _session_ids()
        if not ids:
            print(json.dumps({"ok": True, "removed": []}))
            return
        if not 0 <= index < len(ids):
            print(json.dumps({"ok": False, "error": "Index out of range"}))
            return
        _session_remove([index])
        print(json.dumps({"ok": True, "removed": index}))
        return
    index = _queue_index(args, "Usage: yt-music-ctl queue-remove <index>")
    count = _queue_count()
    if count is None:
        fail("Unable to read playlist")
    if not 0 <= index < count:
        print(json.dumps({"ok": False, "error": "Index out of range"}))
        return
    mpv_send("playlist-remove", index)
    if mpv_is_running():
        write_status_from_mpv(get_mpv_props())
    else:
        write_status({"ok": True, "playing": False})
    # Removing the currently-playing stream must clear the live marker (and
    # any other stream rows); removing a non-current entry leaves a
    # still-current stream alone.
    prune_radio_if_moved_on()
    print(json.dumps({"ok": True, "removed": index}))


def _queue_remove_indices(tokens):
    """Map queue-row keys to playlist indices, or None when unreadable.

    Keys are the panel's row keys: `v:<videoId>#<occurrence>` for YouTube
    entries (the occurrence counts repeats of the same id in queue order) and
    `q:<index>` for local/non-YouTube entries. The playlist is read once and
    every key resolved against it, so a queue advance or an inserted entry
    since the panel built its list cannot shift a key onto the wrong track the
    way a stale index could. Removal is then done in one IPC session.
    """
    props = mpv_query(["playlist"])
    if not isinstance(props, dict):
        return None
    playlist = props.get("playlist")
    if not isinstance(playlist, list):
        return None
    by_key = {}
    seen = {}
    for index, entry in enumerate(playlist):
        if not isinstance(entry, dict):
            continue
        video_id = video_id_from_url(entry.get("filename") or "")
        if valid_video_id(video_id):
            occurrence = seen.get(video_id, 0)
            seen[video_id] = occurrence + 1
            by_key[f"v:{video_id}#{occurrence}"] = index
        else:
            by_key[f"q:{index}"] = index
    indices = []
    for token in tokens:
        index = by_key.get(str(token))
        if index is not None and index not in indices:
            indices.append(index)
    return indices


def cmd_queue_remove_keys(args):
    if not args:
        fail("Usage: yt-music-ctl queue-remove-keys <key...>")
    if not mpv_is_running():
        ids, _index, _pos = _session_ids()
        if not ids:
            print(json.dumps({"ok": True, "removed": []}))
            return
        by_key = {}
        seen = {}
        for i, vid in enumerate(ids):
            occ = seen.get(vid, 0)
            seen[vid] = occ + 1
            by_key["v:%s#%d" % (vid, occ)] = i
        indices = []
        for token in args:
            i = by_key.get(str(token))
            if i is not None and i not in indices:
                indices.append(i)
        print(json.dumps({"ok": True, "removed": _session_remove(indices)}))
        return
    indices = _queue_remove_indices(args)
    if indices is None:
        print(json.dumps({"ok": False, "error": "Unable to read playlist"}))
        return
    if not indices:
        print(json.dumps({"ok": True, "removed": []}))
        return
    removed = []
    # Descending order keeps the lower indices valid as entries disappear.
    for index in sorted(indices, reverse=True):
        resp = mpv_send("playlist-remove", index)
        if isinstance(resp, dict) and resp.get("error") == "success":
            removed.append(index)
    if mpv_is_running():
        write_status_from_mpv(get_mpv_props())
    else:
        write_status({"ok": True, "playing": False})
    # A removed stream row leaves a stale marker unless the stream still plays.
    prune_radio_if_moved_on()
    print(json.dumps({"ok": True, "removed": sorted(removed)}))


def cmd_queue_clear(args):
    """Drop every upcoming queue entry, keeping the current track playing.

    When nothing is playing, drop the saved resume session instead.
    """
    if not mpv_is_running():
        session = load_session()
        ids = session.get("videoIds") if isinstance(session, dict) else None
        removed = len(ids) if isinstance(ids, list) else 0
        clear_session()
        # No player: any live marker is stale by definition.
        clear_radio_current()
        write_status({"ok": True, "playing": False})
        print(json.dumps({"ok": True, "cleared": True, "removed": removed,
                          "remaining": 0, "playing": False}))
        return
    playlist, pos = _playlist_state()
    if not playlist:
        print(json.dumps({"ok": True, "cleared": True, "removed": 0,
                          "remaining": 0, "playing": True}))
        return
    if pos is None or pos < 0:
        pos = 0
    removed = 0
    for index in range(len(playlist) - 1, pos, -1):
        resp = mpv_send("playlist-remove", index)
        if isinstance(resp, dict) and resp.get("error") == "success":
            removed += 1
    write_status_from_mpv(get_mpv_props() or {})
    # Clearing keeps the current entry playing, so the marker survives only if
    # that current entry is still a stream; a song means it is stale.
    prune_radio_if_moved_on()
    playlist, _pos = _playlist_state()
    remaining = len(playlist) if isinstance(playlist, list) else 0
    print(json.dumps({"ok": True, "cleared": True, "removed": removed,
                      "remaining": remaining, "playing": True}))


def cmd_queue_move(args):
    if not mpv_is_running():
        if len(args) < 2:
            fail("Usage: yt-music-ctl queue-move <from> <to>")
        try:
            frm = int(args[0])
            to = int(args[1])
        except ValueError:
            fail("Indexes must be integers")
        ids, index, position = _session_ids()
        count = len(ids)
        if not (0 <= frm < count) or not (0 <= to < count):
            print(json.dumps({"ok": False, "error": "Index out of range"}))
            return
        entry = ids.pop(frm)
        ids.insert(to, entry)
        if index == frm:
            new_index = to
        else:
            new_index = index
            if frm < index:
                new_index -= 1
            if to <= new_index:
                new_index += 1
        save_session(ids, new_index, position)
        print(json.dumps({"ok": True, "from": frm, "to": to}))
        return
    if len(args) < 2:
        fail("Usage: yt-music-ctl queue-move <from> <to>")
    try:
        frm = int(args[0])
        to = int(args[1])
    except ValueError:
        fail("Indexes must be integers")
    count = _queue_count()
    if count is None:
        fail("Unable to read playlist")
    if not (0 <= frm < count) or not (0 <= to < count):
        print(json.dumps({"ok": False, "error": "Index out of range"}))
        return
    # mpv moves an entry to *take the place of* the entry at the target
    # index, so the entry lands one slot earlier when it moves forward.
    # Shifting the target forward (count == append) makes the entry finish
    # exactly at `to`, which is what the user asked for.
    target = to + 1 if frm < to else to
    mpv_send("playlist-move", [frm, target])
    print(json.dumps({"ok": True, "from": frm, "to": to}))


def cmd_stop(args):
    mpv_kill()
    clear_radio_current()
    write_status({"ok": True, "playing": False})
    print(json.dumps({"ok": True}))


def cmd_seek_pct(args):
    if not args:
        fail("Usage: yt-music-ctl seek-pct <0-100>")
    try:
        pct = float(args[0])
    except (TypeError, ValueError):
        fail("Usage: yt-music-ctl seek-pct <0-100>")
    if not mpv_is_running():
        fail("Nothing playing")
    pct = max(0.0, min(100.0, pct))
    mpv_send("seek", [pct, "absolute-percent"])
    props = get_mpv_props()
    write_status_from_mpv(props)
    print(json.dumps({"ok": True}))


def cmd_volume(args):
    if not mpv_is_running():
        return
    if not args:
        props = get_mpv_props()
        return
    try:
        vol = max(0, min(150, int(args[0])))
    except (TypeError, ValueError):
        fail("Usage: yt-music-ctl volume <0-150>")
    save_volume(vol)
    mpv_send("set_property", ["volume", vol])
    props = get_mpv_props()
    write_status_from_mpv(props)
    print(json.dumps({"ok": True, "volume": vol}))


def cmd_loop(args):
    if not mpv_is_running():
        print(json.dumps({"ok": False, "error": "Nothing playing"}))
        return
    requested = (args[0] if args else "inf").strip().lower()
    aliases = {"no": "no", "off": "no", "false": "no",
               "inf": "inf", "on": "inf", "true": "inf", "one": "one"}
    mode = aliases.get(requested)
    if mode is None:
        print(json.dumps({"ok": False,
                          "error": f"Unsupported loop mode: {requested}"}))
        return
    mpv_send("set_property", ["loop-playlist", mode])
    props = get_mpv_props()
    if props:
        write_status_from_mpv(props)
    actual = (props or {}).get("loop-playlist") or mode
    print(json.dumps({"ok": True, "loop": actual}))


def cmd_shuffle(args):
    if not mpv_is_running():
        print(json.dumps({"ok": False, "error": "Nothing playing"}))
        return
    props = get_mpv_props() or {}
    enable = not bool(props.get("shuffle"))
    mpv_send("set_property", ["shuffle", "yes" if enable else "no"])
    # Refresh status so the panel's shuffle button updates immediately.
    new_props = get_mpv_props()
    if new_props:
        write_status_from_mpv(new_props)
    print(json.dumps({"ok": True, "shuffle": enable}))


# ---------------------------------------------------------------- status daemon

OBSERVED_PROPERTIES = [
    "pause", "media-title", "metadata/by-key/artist", "metadata/by-key/album",
    "duration", "time-pos", "volume", "path", "loop-playlist",
    "playlist-pos", "playlist-count", "shuffle",
]


def load_daemon_pid():
    """Read the daemon pidfile without following an attacker-controlled link."""
    try:
        fd = os.open(DAEMON_PID_PATH, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                    or st.st_mode & 0o077):
                return None
            with os.fdopen(fd) as fh:
                fd = None
                return json.load(fh)
        finally:
            if fd is not None:
                os.close(fd)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def unlink_private(path):
    """Remove a 0600 state file owned by this user, and nothing else."""
    try:
        st = os.lstat(path)
        if (stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
                and stat.S_IMODE(st.st_mode) == 0o600):
            os.unlink(path)
    except OSError:
        pass


def daemon_is_running():
    record = load_daemon_pid()
    if not isinstance(record, dict):
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    if not record.get("start_time") or not record.get("executable"):
        return False
    identity = mpv_process_identity(pid)
    if not identity:
        return False
    return (identity[0] == record.get("start_time")
            and identity[1] == record.get("executable"))


def _daemon_log(message):
    """Best-effort append of a timestamped line to DAEMON_LOG. Never raises."""
    try:
        _ensure_private_dir(STATE_DIR)
        fd = os.open(DAEMON_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "a") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


_last_daemon_error = (None, 0.0)


def _daemon_log_throttled(message):
    """_daemon_log, but suppressed until the message changes or 60s passes."""
    global _last_daemon_error
    last_message, last_time = _last_daemon_error
    now = time.time()
    if message == last_message and now - last_time <= 60.0:
        return
    _last_daemon_error = (message, now)
    _daemon_log(message)


_REFUSED_MPV_SOCKET_UNSET = object()
_refused_mpv_socket_signature = _REFUSED_MPV_SOCKET_UNSET


def _log_refused_mpv_socket():
    """Log once per distinct socket that the daemon refused to attach to."""
    global _refused_mpv_socket_signature
    try:
        st = os.lstat(MPV_SOCKET)
        signature = (st.st_ino, st.st_mtime_ns)
    except OSError:
        signature = None
    if signature == _refused_mpv_socket_signature:
        return
    _refused_mpv_socket_signature = signature
    _daemon_log(
        f"refused to attach to {MPV_SOCKET}: mpv identity did not match"
    )


def ensure_daemon():
    """Start the detached status daemon when it is missing or stale."""
    try:
        script_mtime = os.path.getmtime(os.path.realpath(__file__))
        if daemon_is_running():
            record = load_daemon_pid() or {}
            if record.get("script_mtime") == script_mtime:
                return False
            daemon_stop()
        _ensure_private_dir(STATE_DIR)
        fd = os.open(DAEMON_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "ab") as log:
            subprocess.Popen(
                [sys.executable, os.path.realpath(__file__), "daemon"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        return True
    except Exception:
        return False


def daemon_stop():
    """SIGTERM the verified daemon process. Never raises."""
    stopped = False
    try:
        record = load_daemon_pid()
        pid = record.get("pid") if isinstance(record, dict) else None
        identity = mpv_process_identity(pid) if isinstance(pid, int) and pid > 1 else None
        identity_matches = (
            identity is not None
            and identity[0] == record.get("start_time")
            and identity[1] == record.get("executable")
        )
        if identity_matches:
            pidfd = None
            try:
                pidfd = os.pidfd_open(pid) if hasattr(os, "pidfd_open") else None
                # Recheck after opening the pidfd so a dead process cannot be
                # confused with a newly reused PID.
                if mpv_process_identity(pid) == identity:
                    if pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                    else:
                        os.kill(pid, signal.SIGTERM)
                    stopped = True
                    deadline = time.time() + 1
                    while time.time() < deadline:
                        if mpv_process_identity(pid) != identity:
                            break
                        time.sleep(0.05)
                    if mpv_process_identity(pid) == identity:
                        if pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                        else:
                            os.kill(pid, signal.SIGKILL)
                        deadline = time.time() + 1
                        while time.time() < deadline:
                            if mpv_process_identity(pid) != identity:
                                break
                            time.sleep(0.05)
            except OSError:
                pass
            finally:
                if pidfd is not None:
                    try:
                        os.close(pidfd)
                    except OSError:
                        pass
        if not daemon_is_running():
            unlink_private(DAEMON_PID_PATH)
            unlink_private(DAEMON_LOCK)
    except Exception:
        pass
    return stopped


def monitor_mpv_events():
    """Stream mpv property changes into status.json until the socket dies."""
    if not managed_mpv_identity_matches(load_mpv_pid() or {}):
        _log_refused_mpv_socket()
        return
    cache = get_mpv_props()
    if not cache:
        return
    last_signature = None
    notify_key = None
    notify_deadline = 0.0
    current_title = ""
    precache_retry_at = 0.0
    precache_retry_id = ""
    # Snapshot of the last status that is safe to compare against: the ungated
    # status write would otherwise put the new (videoId, title) into the file
    # during a hold and make notify_track_change's backstop swallow the
    # deferred notification.
    notify_previous = read_status()

    def flush():
        nonlocal last_signature, notify_key, notify_previous
        nonlocal precache_retry_at, precache_retry_id
        props = dict(cache)
        for key, default in (("pause", True), ("media-title", ""), ("path", ""),
                             ("volume", 100), ("duration", 0), ("time-pos", 0)):
            if props.get(key) is None:
                props[key] = default
        # Retry precache on a timer as well as on property changes: the very
        # first flush after mpv starts can race the playlist being populated,
        # and a signature that never changes again would otherwise skip it.
        retry_id = str(props.get("path") or "")
        now = time.time()
        if retry_id and retry_id != precache_retry_id:
            precache_retry_id = retry_id
            precache_retry_at = now + 3.0
        elif retry_id and now >= precache_retry_at:
            precache_retry_at = now + 3.0
            current_id = extract_video_id(props)
            if valid_video_id(current_id):
                schedule_precache(current_id)
        try:
            signature = (
                not bool(props.get("pause", True)),
                str(props.get("media-title") or ""),
                str(props.get("metadata/by-key/artist") or ""),
                str(props.get("metadata/by-key/album") or ""),
                str(props.get("path") or ""),
                str(props.get("loop-playlist") or "no"),
                round(float(props.get("volume", 100))),
                round(float(props.get("duration", 0))),
                round(float(props.get("time-pos", 0))),
                str(props.get("playlist-pos")),
                str(props.get("playlist-count")),
            )
        except (TypeError, ValueError):
            signature = None
        if signature is not None and signature == last_signature:
            return
        previous = notify_previous
        status = write_status_from_mpv(props, notify=False)
        last_signature = signature
        if not status or not status.get("playing"):
            notify_previous = status
            return
        title = status.get("title") or ""
        video_id = status.get("videoId") or ""
        if video_id and status.get("playing"):
            # Warm the next queue entry's audio off-thread whenever the
            # playing track changes (dedup + one-at-a-time live in there).
            schedule_precache(video_id)
        if not video_id or _looks_like_url_title(title):
            notify_previous = status
            return
        key = (video_id, title)
        if key == notify_key:
            notify_previous = status
            return
        if status.get("artist") or status.get("album"):
            notify_track_change(previous, status)
            notify_key = key
            notify_previous = status
        elif time.time() >= notify_deadline:
            notify_track_change(previous, status)
            notify_key = key
            notify_previous = status
        else:
            return

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2)
        sock.connect(MPV_SOCKET)
        for index, name in enumerate(OBSERVED_PROPERTIES, start=1):
            cmd = json.dumps({"command": ["observe_property", index, name]}) + "\n"
            sock.sendall(cmd.encode())
        sock.settimeout(1.0)
        flush()
        buffer = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                flush()
                continue
            if not chunk:
                break
            buffer += chunk
            lines = buffer.split(b"\n")
            buffer = lines[-1]
            changed = False
            shutdown = False
            for line in lines[:-1]:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("event")
                if kind == "property-change":
                    name = event.get("name")
                    if isinstance(name, str):
                        cache[name] = event.get("data")
                        changed = True
                        if name == "media-title":
                            value = event.get("data")
                            if (isinstance(value, str) and value
                                    and not _looks_like_url_title(value)
                                    and value != current_title):
                                current_title = value
                                notify_deadline = time.time() + 1.2
                elif kind == "shutdown":
                    shutdown = True
            if changed:
                flush()
            if shutdown:
                break
    except Exception as e:
        _daemon_log_throttled(f"monitor_mpv_events: {e!r}")
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
        flush()


def run_daemon_loop():
    """Own mpv IPC monitoring until interrupted; singleton via flock."""
    lock_fd = None
    try:
        try:
            lock_fd = os.open(DAEMON_LOCK, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
            st = os.fstat(lock_fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise OSError("unsafe daemon lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
            return
        signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
        try:
            record = {"pid": os.getpid()}
            identity = mpv_process_identity(os.getpid())
            if identity:
                record["start_time"] = identity[0]
                record["executable"] = identity[1]
            record["script_mtime"] = os.path.getmtime(os.path.realpath(__file__))
            json_dump(DAEMON_PID_PATH, record)
        except Exception:
            pass
        idle = False
        while True:
            try:
                if not mpv_is_running():
                    if not idle:
                        write_status({"ok": True, "playing": False})
                        idle = True
                    time.sleep(0.5)
                    continue
                idle = False
                # Kick the next-track precache from the loop as well as from
                # flush(): if the daemon attached to a fresh mpv mid-startup,
                # a flush may have fired before the playlist was populated.
                # schedule_precache is idempotent and cheap when it has
                # nothing to do.
                _playing_id = extract_video_id(get_mpv_props() or {})
                if valid_video_id(_playing_id):
                    schedule_precache(_playing_id)
                monitor_mpv_events()
                if not mpv_is_running():
                    write_status({"ok": True, "playing": False})
                    idle = True
                else:
                    time.sleep(0.2)
            except Exception as e:
                _daemon_log_throttled(f"run_daemon_loop: {e!r}")
                time.sleep(0.5)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass
        try:
            record = load_daemon_pid()
            if isinstance(record, dict) and record.get("pid") == os.getpid():
                unlink_private(DAEMON_PID_PATH)
        except Exception:
            pass


def cmd_daemon(args):
    _ensure_private_dir(STATE_DIR)
    try:
        run_daemon_loop()
    except KeyboardInterrupt:
        pass


def cmd_ensure_daemon(args):
    started = ensure_daemon()
    if started:
        deadline = time.time() + 2
        while time.time() < deadline and not daemon_is_running():
            time.sleep(0.05)
    print(json.dumps({"ok": True, "started": bool(started),
                      "running": daemon_is_running()}))


def cmd_daemon_stop(args):
    stopped = daemon_stop()
    print(json.dumps({"ok": True, "stopped": bool(stopped)}))


# Cached-read commands a detached background refresher may re-run. Kept out
# of the help text: "__refresh" is internal plumbing, not a user command.
REFRESHABLE_COMMANDS = ("liked", "library", "home", "history", "album",
                        "artist", "search", "playlist", "mix", "radio", "lyrics")


def _refresh_mix_metadata(args):
    """Repopulate a stale mix cache entry without touching playback.

    Mirrors cmd_mix's key (list(args)) and payload exactly, but omits
    _mix_launch: the refresher runs detached while the user is already
    listening, so it must never kill or restart mpv.
    """
    try:
        if not args:
            return
        seed_id = args[0]
        key = list(args)
        ytm = get_ytmusic(require_auth=False)
        watchlist = ytm.get_watch_playlist(seed_id, limit=50)
        tracks = []
        for track in (watchlist.get("tracks") or []):
            vid = track.get("videoId", "")
            if not vid:
                continue
            album = track.get("album") or {}
            if not isinstance(album, dict):
                album = {"name": album}
            tracks.append({
                "videoId": vid,
                "title": track.get("title", ""),
                "artist": ", ".join(a.get("name", "") for a in (track.get("artists") or [])),
                "album": album.get("title", "") or album.get("name", ""),
                "duration": track.get("duration_seconds", 0) or 0,
            })
        if not tracks:
            return
        payload = {"seedId": seed_id, "trackList": tracks}
        cache_write("mix", key, payload)
    except Exception:
        pass
    except SystemExit:
        pass


def _refresh_radio_metadata(args):
    """Repopulate a stale radio cache entry without touching playback.

    Mirrors cmd_artist_radio's key ([browse_id]) and payload exactly, but
    omits _mix_launch and any output: metadata only.
    """
    try:
        if not args:
            return
        browse_id = args[0]
        key = [browse_id]
        ytm = get_ytmusic(require_auth=True)
        artist = ytm.get_artist(browse_id)
        radio_id = artist.get("radioId", "") or ""
        if not radio_id:
            return
        watchlist = ytm.get_watch_playlist(playlistId=radio_id, radio=True)
        tracks = []
        for track in (watchlist.get("tracks") or []):
            vid = track.get("videoId", "")
            if not vid:
                continue
            album = track.get("album") or {}
            if not isinstance(album, dict):
                album = {"name": album}
            tracks.append({
                "videoId": vid,
                "title": track.get("title", ""),
                "artist": ", ".join(a.get("name", "") for a in (track.get("artists") or [])),
                "album": album.get("title", "") or album.get("name", ""),
                "duration": track.get("duration_seconds", 0) or 0,
            })
        if not tracks:
            return
        payload = {"ok": True, "mix": True, "seedId": browse_id,
                   "radioId": radio_id, "trackList": tracks, "cached": False}
        cache_write("radio", key, payload)
    except Exception:
        pass
    except SystemExit:
        pass


# mpv-mpris loads from an mpv script directory; install.sh's non-fatal notice
# and scripts/smoke.sh probe the very same three locations.
MPRIS_SCRIPT_CANDIDATES = (
    "/etc/mpv/scripts/mpris.so",
    "/usr/lib/mpv-mpris/mpris.so",
    os.path.expanduser("~/.config/mpv/scripts/mpris.so"),
)


def mpris_script_path():
    """First mpv-mpris script that exists (symlinks followed), else None."""
    for candidate in MPRIS_SCRIPT_CANDIDATES:
        try:
            if os.path.exists(candidate):
                return candidate
        except OSError:
            continue
    return None


def cmd_doctor(args):
    """Print a JSON report of the desktop-integration prerequisites.

    mpris is what exposes playback on D-Bus (org.mpris.MediaPlayer2.mpv) so
    the media keys and Omarchy's media widget can control this player. cava
    drives the optional audio visualizer. Pure filesystem/PATH checks: no
    subprocesses, nothing that can fail loudly.
    """
    script = mpris_script_path()
    mpris = script is not None
    cava = shutil.which("cava") is not None
    hints = []
    if not mpris:
        hints.append("sudo pacman -S mpv-mpris")
    if not cava:
        hints.append("sudo pacman -S cava (audio visualizer)")
    print(json.dumps({
        "ok": True,
        "mpris": mpris,
        "mpris_script": script,
        "mpv": shutil.which("mpv") is not None,
        "yt_dlp": shutil.which("yt-dlp") is not None,
        "cava": cava,
        "python": shutil.which("python3") is not None,
        "hint": " · ".join(hints),
    }))


def cmd_internal_refresh(args):
    """Re-run one cached-read command with -r to repopulate a stale entry.

    Spawned by spawn_background_refresh with stdout already at /dev/null, so
    the JSON the command prints is discarded. It must never fail loudly:
    anything else, including fail()'s SystemExit, is swallowed.

    mix and radio carry a playback side effect on the normal serve path, so
    they are refreshed through metadata-only helpers instead of the command
    itself: the detached child must never restart the user's audio.
    """
    if len(args) < 1 or args[0] not in REFRESHABLE_COMMANDS:
        return
    name = args[0]
    sub_args = list(args[1:])
    try:
        if name == "mix":
            _refresh_mix_metadata(sub_args)
        elif name == "radio":
            _refresh_radio_metadata(sub_args)
        else:
            COMMANDS[name](sub_args + ["-r"])
    except Exception:
        pass
    except SystemExit:
        pass


# ---------------------------------------------------------------- main

COMMANDS = {
    "login": cmd_login,
    "logout": cmd_logout,
    "status": cmd_status,
    "play": cmd_play,
    "play-next": cmd_play_next,
    "queue-add": cmd_queue_add,
    "playlist-add": cmd_playlist_add,
    "playlist-add-items": cmd_playlist_add_items,
    "playlist-edit": cmd_playlist_edit,
    "playlist-delete": cmd_playlist_delete,
    "playlist-move": cmd_playlist_move,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "toggle": cmd_toggle,
    "next": cmd_next,
    "prev": cmd_prev,
    "seek": cmd_seek,
    "seek-pct": cmd_seek_pct,
    "volume": cmd_volume,
    "stop": cmd_stop,
    "like": cmd_like,
    "dislike": cmd_dislike,
    "unlike": cmd_unlike,
    "playlists": cmd_playlists,
    "create-playlist": cmd_create_playlist,
    "playlist": cmd_playlist_tracks,
    "remove": cmd_remove,
    "search": cmd_search,
    "liked": cmd_liked,
    "library": cmd_library,
    "album": cmd_album,
    "album-status": cmd_album_status,
    "album-save": cmd_album_save,
    "album-remove": cmd_album_remove,
    "artist": cmd_artist,
    "radio": cmd_artist_radio,
    "home": cmd_home,
    "station-catalog": cmd_station_catalog,
    "station-search": cmd_station_search,
    "station-favorites": cmd_station_favorites,
    "station-fav-add": cmd_station_fav_add,
    "station-fav-remove": cmd_station_fav_remove,
    "station-featured-add": cmd_station_featured_add,
    "station-featured-remove": cmd_station_featured_remove,
    "station-play": cmd_station_play,
    "station-history": cmd_station_history,
    "history": cmd_history,
    "last-played": cmd_last_played,
    "restore": cmd_restore,
    "enqueue": cmd_enqueue,
    "enqueue-files": cmd_enqueue_files,
    "thumbnail": cmd_thumbnail,
    "image": cmd_image,
    "precache": cmd_precache,
    "lyrics": cmd_lyrics,
    "mix": cmd_mix,
    "queue": cmd_queue_playlist,
    "queue-list": cmd_queue_list,
    "queue-clear": cmd_queue_clear,
    "queue-jump": cmd_queue_jump,
    "queue-remove": cmd_queue_remove,
    "queue-remove-keys": cmd_queue_remove_keys,
    "queue-move": cmd_queue_move,
    "loop": cmd_loop,
    "shuffle": cmd_shuffle,
    "daemon": cmd_daemon,
    "watch": cmd_daemon,
    "ensure-daemon": cmd_ensure_daemon,
    "daemon-stop": cmd_daemon_stop,
    "doctor": cmd_doctor,
    "__refresh": cmd_internal_refresh,
}


def main():
    if len(sys.argv) < 2:
        print("yt-music-ctl — YouTube Music bar widget backend")
        print()
        print("Commands:")
        print("  login                    Log in via browser")
        print("  status                   Update status from mpv")
        print("  play <videoId>           Play a song")
        print("  play-next <videoId>      Insert track to play next")
        print("  pause                    Pause playback")
        print("  resume                   Resume playback")
        print("  toggle                   Toggle play/pause")
        print("  next                     Next track")
        print("  prev                     Previous track")
        print("  seek <seconds>           Seek relative")
        print("  seek-pct <0-100>         Seek to percentage")
        print("  volume <0-150>           Set volume")
        print("  stop                     Stop playback")
        print("  logout                   Remove local YouTube Music authentication")
        print("  like <videoId>           Like a song (adds it to Liked Music)")
        print("  dislike <videoId>        Dislike a song and drop it from your playlists")
        print("  unlike <videoId>         Remove your like")
        print("  playlists                List library playlists")
        print("  create-playlist <name>   Create a private playlist")
        print("  playlist <playlistId>    Get playlist tracks")
        print("  playlist-add <id> <vid...>  Add one or more tracks to a playlist (LM = liked)")
        print("  playlist-add-items <id> <v:id|a:album|r:artist|p:playlist...>  Add tracks/albums/artists/playlists to a playlist")
        print("  playlist-edit <id> [--title T] [--description D] [--privacy P]  Edit a playlist")
        print("  playlist-delete <id>      Delete a playlist")
        print("  playlist-move <id> <from> <to>  Reorder a playlist track")
        print("  search [-f kind] <query>  Search songs|albums|artists|playlists")
        print("  liked [limit]            List liked songs")
        print("  library <kind> [limit]   List library songs|albums|artists|playlists")
        print("  home [sections]          Fetch the home feed (default 3 sections)")
        print("  station-catalog           List the (editable) Featured radio stations (JSON)")
        print("  station-search [--tag] <query>  Search Radio Browser "
              "by name or genre [--limit N]")
        print("  station-favorites         List saved radio stations (JSON)")
        print("  station-fav-add '<json>|<url> [name]'   Save a radio station to favorites")
        print("  station-fav-remove <id>   Remove a saved radio station")
        print("  station-featured-add '<json>|<url> [name]'   Add a station to Featured")
        print("  station-featured-remove <id>   Remove a station from Featured")
        print("  station-play '<json>|<url>|<id>' [name]  Play an internet radio station")
        print("  station-history [limit|clear]   Recently played radio stations")
        print("  history [limit]          List recently played tracks")
        print("  last-played [limit|clear] Local play history")
        print("  restore                  Rebuild the last queue, paused")
        print("  album <browseId>         Get album tracks")
        print("  album-status <browseId>  Report whether an album is saved to your library")
        print("  album-save <browseId>    Save an album to your library")
        print("  album-remove <browseId>  Remove an album from your library")
        print("  artist <browseId>        Get an artist's top songs + albums")
        print("  enqueue <play|queue|next> <album|artist|playlist> <id>   Play/queue a whole album, artist or playlist")
        print("  enqueue-files <play|queue|next> <videoId...>   Play/queue an explicit list of videoIds")
        print("  thumbnail <videoId>     Fetch a bounded album thumbnail")
        print("  image <url>             Cache an album/artist cover image")
        print("  precache [videoId]      Prefetch next-track audio (or one id)")
        print("  lyrics <videoId>        Fetch lyrics (plain, plus synced when available)")
        print("  mix <videoId>            Play radio mix from seed")
        print("  radio <browseId>         Play an artist's radio (auto-mix)")
        print("  queue <playlistId>       Queue and play a playlist")
        print("  queue-add <videoId>      Append a track to the queue")
        print("  queue-list               List the current queue")
        print("  queue-clear              Remove every upcoming track")
        print("  queue-jump <index>       Jump to a queue index")
        print("  queue-remove <index>     Remove a queue entry")
        print("  queue-remove-keys <key...>  Remove queue entries by row key")
        print("  queue-move <from> <to>   Move a queue entry")
        print("  loop <mode>              Set loop mode (off/inf)")
        print("  shuffle                  Shuffle current playlist")
        print("  daemon                   Run the status daemon in the foreground")
        print("  ensure-daemon            Start the status daemon in the background")
        print("  daemon-stop              Stop the status daemon")
        print("  doctor                   Check media-key/MPRIS integration (JSON)")
        print("  watch                    Legacy alias for daemon")
        print()
        print("Metadata read commands (search, library, home, history, last-played, album,")
        print("artist, playlist, mix, lyrics) accept -r/--refresh anywhere to")
        print("bypass the on-disk metadata cache.")
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        fail(f"Unknown command: {cmd}")
    try:
        COMMANDS[cmd](sys.argv[2:])
    except SystemExit:
        raise
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        fail(str(e))


if __name__ == "__main__":
    main()
