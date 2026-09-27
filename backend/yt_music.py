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
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

STATE_DIR = os.path.expanduser("~/.local/state/yt-music")
CONFIG_DIR = os.path.expanduser("~/.config/yt-music")
STATUS_PATH = os.path.join(STATE_DIR, "status.json")
TRACK_META_PATH = os.path.join(STATE_DIR, "track-meta.json")
LAST_PLAYED_PATH = os.path.join(STATE_DIR, "last-played.json")
SESSION_PATH = os.path.join(STATE_DIR, "session.json")
LAST_PLAYED_MAX = 200
SESSION_SAVE_INTERVAL = 5
TRACK_META_MAX = 500
DAEMON_LOCK = os.path.join(STATE_DIR, "daemon.lock")
DAEMON_PID_PATH = os.path.join(STATE_DIR, "daemon.pid")
DAEMON_LOG = os.path.join(STATE_DIR, "daemon.log")
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
MPV_RUNTIME_DIR = os.path.join(RUNTIME_DIR, "yt-music")
MPV_SOCKET = os.path.join(MPV_RUNTIME_DIR, "mpv.sock")
MPV_PID_PATH = os.path.join(MPV_RUNTIME_DIR, "mpv.pid")
LIKES_TITLE = "Liked Music"
THUMBNAIL_CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "yt-music", "thumbs")
IMAGE_CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "yt-music", "images")
IMAGE_HOST_SUFFIXES = ("googleusercontent.com", "ytimg.com", "ggpht.com", "google.com")
MAX_THUMBNAIL_BYTES = 1024 * 1024
MAX_THUMBNAIL_DIMENSION = 4096
MAX_THUMBNAIL_PIXELS = 16 * 1024 * 1024

# Precached audio for queue tracks: one file per videoId under
# $XDG_CACHE_HOME/yt-music/audio (files 0600, directory 0700). The cap covers
# every file in the directory; the oldest (by mtime) are evicted first.
AUDIO_CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "yt-music", "audio")
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
    "lyrics": 86400,   # 24 h — lyrics essentially never change
}


# ---------------------------------------------------------------- helpers

def fail(msg, code=1):
    print(f"yt-music-ctl: {msg}", file=sys.stderr)
    sys.exit(code)


def valid_video_id(value):
    return (isinstance(value, str) and len(value) == 11 and
            all(c.isalnum() or c in "_-" for c in value))


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
    os.makedirs(os.path.dirname(path), exist_ok=True)
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
    return os.path.join(METADATA_CACHE_DIR, _cache_key(namespace, args) + ".json")


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
        os.makedirs(METADATA_CACHE_DIR, mode=0o700, exist_ok=True)
        try:
            os.chmod(METADATA_CACHE_DIR, 0o700)
        except OSError:
            pass
        record = {"ts": time.time(), "payload": payload}
        json_dump(_cache_path(namespace, args), record)
        _cache_prune()
    except Exception:
        pass


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
    one refresher each: while refresh.lock is younger than 120 s nothing new
    is spawned, and the child deliberately never touches it (the staleness
    window makes deleting it racy). Never raises.
    """
    try:
        lock = os.path.join(STATE_DIR, "refresh.lock")
        try:
            if time.time() - os.lstat(lock).st_mtime < 120:
                return False
        except OSError:
            pass
        try:
            os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
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


def refresh_auth_headers(auth):
    """Refresh the timestamped auth signature before using stored cookies."""
    if not isinstance(auth, dict):
        return auth

    cookie_header = auth.get("Cookie", "")
    cookies = {}
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        cookies[name] = value

    sapisid = cookies.get("__Secure-3PAPISID") or cookies.get("SAPISID")
    if not sapisid:
        return auth

    import hashlib
    origin = auth.get("Origin") or auth.get("X-Origin") or "https://music.youtube.com"
    ts = str(int(time.time()))
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    auth["Authorization"] = f"SAPISIDHASH {ts}_{digest}"
    return auth


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
        for entry in playlist:
            path = entry.get("filename") if isinstance(entry, dict) else ""
            path = path or ""
            vid = video_id_from_url(path) if path else ""
            if not valid_video_id(vid):
                vid = cache_audio_video_id(path) if path else ""
            if not valid_video_id(vid):
                # An entry we cannot resolve would make the resumed queue lossy.
                return
            ids.append(vid)
        if not ids:
            return
        if pos < 0 or pos >= len(ids):
            pos = ids.index(video_id) if video_id in ids else 0
        save_session(ids, pos, status.get("position") or 0)
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


def get_ytmusic(require_auth=True):
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
    refresh_auth_headers(auth)

    # Browser cookies can expire while the local auth file still exists. In
    # that case YouTube returns an anonymous library page instead of an error,
    # which otherwise looks like an empty playlist collection.
    if not validate_auth(auth):
        fresh_auth = build_browser_auth()
        if fresh_auth and validate_auth(fresh_auth):
            auth = fresh_auth
        else:
            fail("YouTube session expired. Run: yt-music-ctl login")
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
    clear_session()
    # Shut down a pre-runtime-dir instance on the first managed replacement.
    if private_mpv_socket():
        mpv_send("quit")
    pid_data = load_mpv_pid() or {}
    pid = pid_data.get("pid")
    identity = mpv_process_identity(pid) if isinstance(pid, int) and pid > 1 else None
    expected_executable = os.path.realpath(shutil.which("mpv") or "")
    identity_matches = (
        identity is not None
        and identity[0] == pid_data.get("start_time")
        and identity[1] == pid_data.get("executable") == expected_executable
        and pid_data.get("socket") == MPV_SOCKET
    )
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
    ensure_daemon()
    mpv_kill()
    ensure_private_runtime_dir()
    url = f"https://music.youtube.com/watch?v={video_id}"
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
        url
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


def video_id_from_url(url):
    """Pull the video id out of a watch/youtu.be URL (or a bare id)."""
    if not isinstance(url, str) or not url:
        return None
    # A precached queue entry plays as a local file rather than a URL.
    local = cache_audio_video_id(url)
    if local:
        return local
    if "v=" in url:
        for part in url.split("?"):
            if "v=" in part:
                return part.split("v=")[1].split("&")[0]
    if "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0]
    if "youtube.com/watch" in url:
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        return qs.get("v", [None])[0]
    return None


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
    video_id = extract_video_id(props)
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
    if status["playing"] and video_id and video_id != str(previous.get("videoId") or ""):
        sidecar = load_track_meta().get(video_id) or {}
        remember_play({
            "videoId": video_id,
            "title": status.get("title") or sidecar.get("title") or "",
            "artist": status.get("artist") or sidecar.get("artist") or "",
            "album": status.get("album") or sidecar.get("album") or "",
            "duration": status.get("duration") or sidecar.get("duration") or 0,
        })
    maybe_save_session(status)
    if notify:
        notify_track_change(previous, status)
    write_status(status)
    # A one-shot CLI playback start schedules the next-track precache in a
    # detached child, so the first warm does not depend on when the daemon
    # happens to attach to the new mpv. The daemon never sets this flag.
    if spawn_precache and status.get("playing") and video_id:
        spawn_precache_next()
    return status


# ---------------------------------------------------------------- commands

def cmd_login(args):
    auth_path = os.path.join(CONFIG_DIR, "auth.json")
    os.makedirs(CONFIG_DIR, exist_ok=True)

    # try to read auth straight from the browser cookies — no manual paste
    if "--manual" not in args:
        print("Reading YouTube cookies from your browser...")
        auth = build_browser_auth()
        if auth:
            if validate_auth(auth):
                json_dump(auth_path, auth)
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
    print(f"Auth saved to {auth_path}")
    print("You can now use yt-music-ctl commands: playlists, search, play, mix")


def cmd_status(args):
    if not mpv_is_running():
        write_status({"ok": True, "playing": False})
        return
    props = get_mpv_props()
    if not props:
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
    mpv_play(video_id)
    props = wait_for_metadata()
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
    url = f"https://music.youtube.com/watch?v={video_id}"
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
    url = f"https://music.youtube.com/watch?v={video_id}"
    remember_tracks([entry])
    mpv_send("loadfile", [url, "append"])
    print(json.dumps({"ok": True, "queued": True, "videoId": video_id}))


def cmd_playlist_add(args):
    if len(args) < 2:
        fail("Usage: yt-music-ctl playlist-add <playlistId> <videoId>")
    playlist_id, video_id = args[0], args[1]
    if not valid_video_id(video_id):
        fail("Invalid video ID")
    ytm = get_ytmusic()
    try:
        # Liked Music is a system playlist; edit it via the song rating.
        if playlist_id == "LM":
            ytm.rate_song(video_id, "LIKE")
        else:
            ytm.add_playlist_items(playlist_id, [video_id])
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return
    print(json.dumps({"ok": True, "added": True, "playlistId": playlist_id,
                      "videoId": video_id}))


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


def cmd_toggle(args):
    if not mpv_is_running():
        fail("Nothing playing")
    props = get_mpv_props()
    if props:
        paused = props.get("pause", True)
        mpv_send("set_property", ["pause", not paused])
        props2 = get_mpv_props()
        write_status_from_mpv(props2)


def cmd_next(args):
    if not mpv_is_running():
        fail("Nothing playing")
    mpv_send("playlist-next", "force")
    time.sleep(1)
    props = get_mpv_props()
    write_status_from_mpv(props)
    print(json.dumps({"ok": True}))


def cmd_prev(args):
    if not mpv_is_running():
        fail("Nothing playing")
    mpv_send("playlist-prev", "force")
    time.sleep(1)
    props = get_mpv_props()
    write_status_from_mpv(props)
    print(json.dumps({"ok": True}))


def cmd_seek(args):
    if not mpv_is_running():
        fail("Nothing playing")
    if not args:
        fail("Usage: yt-music-ctl seek <seconds>")
    seconds = int(args[0])
    mpv_send("seek", [seconds, "relative"])
    props = get_mpv_props()
    write_status_from_mpv(props)
    print(json.dumps({"ok": True}))


def cmd_like(args):
    if not args:
        fail("Usage: yt-music-ctl like <videoId>")
    video_id = args[0]
    ytm = get_ytmusic()
    try:
        ytm.rate_song(video_id, "LIKE")
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return
    try:
        playlists = ytm.get_library_playlists(limit=50)
        likes_pl = None
        for pl in playlists:
            title = (pl.get("title") or "").strip().lower()
            if title == LIKES_TITLE.lower():
                likes_pl = pl
                break
        added = False
        if likes_pl:
            ytm.add_playlist_items(likes_pl["playlistId"], [video_id])
            added = True
        else:
            new_pl = ytm.create_playlist(LIKES_TITLE, "Liked from YouTube Music")
            ytm.add_playlist_items(new_pl["playlistId"], [video_id])
            added = True
        print(json.dumps({"ok": True, "liked": True, "addedToPlaylist": added}))
    except Exception as e:
        print(json.dumps({"ok": True, "liked": True, "addedToPlaylist": False,
                          "error": str(e)}))


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
                tracks = ytm.get_playlist(playlist_id, limit=100).get("tracks") or []
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
        print(json.dumps({"ok": True}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_playlists(args):
    ytm = get_ytmusic()
    try:
        playlists = ytm.get_library_playlists(limit=50)
        result = []
        for pl in playlists:
            result.append({
                "id": pl.get("playlistId", ""),
                "title": pl.get("title", ""),
                "count": len(pl.get("thumbnails") or []),
                "description": pl.get("description", ""),
            })
        print(json.dumps({"ok": True, "playlists": result}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_create_playlist(args):
    if not args:
        fail("Usage: yt-music-ctl create-playlist <name>")
    title = " ".join(args).strip()
    if not title:
        fail("Playlist name cannot be empty")
    ytm = get_ytmusic()
    try:
        playlist_id = ytm.create_playlist(title, "", "PRIVATE")
        if isinstance(playlist_id, dict):
            playlist_id = playlist_id.get("playlistId", "")
        print(json.dumps({"ok": True, "id": playlist_id, "title": title}))
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
    ytm = _ytmusic_for_cache("playlist", key, ttl)
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
            print(json.dumps({"ok": True, "removed": 1}))
            return

        playlist = ytm.get_playlist(playlist_id, limit=100)
        matches = [
            {"videoId": track["videoId"], "setVideoId": track["setVideoId"]}
            for track in (playlist.get("tracks") or [])
            if track.get("videoId") == video_id and track.get("setVideoId")
        ]
        if not matches:
            print(json.dumps({"ok": False, "error": "Track is not removable from this playlist"}))
            return
        ytm.remove_playlist_items(playlist_id, matches)
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
    ytm = _ytmusic_for_cache("liked", key, ttl)
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
    ytm = _ytmusic_for_cache("library", key, ttl)
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
    ytm = _ytmusic_for_cache("album", key, ttl)
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
    ytm = _ytmusic_for_cache("artist", key, ttl)
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
    ytm = _ytmusic_for_cache("radio", key, ttl)
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
    ytm = _ytmusic_for_cache("home", key, ttl)
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
    ytm = _ytmusic_for_cache("history", key, ttl)
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


def cmd_restore(args):
    """Rebuild the last queue paused at its saved index + position."""
    if mpv_is_running():
        print(json.dumps({"ok": True, "restored": False, "reason": "already-playing"}))
        return
    session = load_session()
    ids = session.get("videoIds") if isinstance(session, dict) else None
    ids = [v for v in ids if valid_video_id(v)] if isinstance(ids, list) else []
    if not ids:
        print(json.dumps({"ok": True, "restored": False, "reason": "no-session"}))
        return
    try:
        index = max(0, min(len(ids) - 1, int(session.get("index") or 0)))
    except (TypeError, ValueError):
        index = 0
    try:
        position = max(0.0, float(session.get("position") or 0))
    except (TypeError, ValueError):
        position = 0.0
    ensure_daemon()
    ensure_private_runtime_dir()
    urls = [f"https://music.youtube.com/watch?v={v}" for v in ids]
    proc = subprocess.Popen(
        ["mpv", "--no-video", "--really-quiet",
         f"--input-ipc-server={MPV_SOCKET}", "--keep-open=no", "--pause=yes"] + urls,
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
    mpv_send("set_property", ["pause", True])
    props = wait_for_metadata()
    if isinstance(props, dict):
        props = dict(props)
        props["pause"] = True
    write_status_from_mpv(props)
    print(json.dumps({"ok": True, "restored": True, "count": len(ids), "index": index}))


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
    ytm = _ytmusic_for_cache("lyrics", key, ttl)
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


def cmd_thumbnail(args):
    if not args or not valid_video_id(args[0]):
        fail("Invalid video ID")
    video_id = args[0]
    os.makedirs(THUMBNAIL_CACHE_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(THUMBNAIL_CACHE_DIR, f"{video_id}.jpg")
    try:
        request = urllib.request.Request(
            f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            headers={"User-Agent": "yt-music-ctl/1"})
        with urllib.request.urlopen(request, timeout=8) as response:
            data = response.read(MAX_THUMBNAIL_BYTES + 1)
        if len(data) > MAX_THUMBNAIL_BYTES:
            fail("Thumbnail is too large")
        dimensions = jpeg_dimensions(data)
        if (not dimensions or dimensions[0] > MAX_THUMBNAIL_DIMENSION or
                dimensions[1] > MAX_THUMBNAIL_DIMENSION or
                dimensions[0] * dimensions[1] > MAX_THUMBNAIL_PIXELS):
            fail("Thumbnail dimensions are not allowed")
        fd, temporary = tempfile.mkstemp(dir=THUMBNAIL_CACHE_DIR, prefix=".thumb-", suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except Exception as exc:
        fail(f"Thumbnail fetch failed: {exc}")


def cmd_image(args):
    if not args or not args[0].startswith(("http://", "https://")):
        fail("Usage: yt-music-ctl image <url>")
    url = args[0]
    import urllib.parse
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if not any(host == suffix or host.endswith("." + suffix) for suffix in IMAGE_HOST_SUFFIXES):
        fail("Image host not allowed")
    os.makedirs(IMAGE_CACHE_DIR, mode=0o700, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()[:32]
    path = os.path.join(IMAGE_CACHE_DIR, f"{key}.jpg")
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return
        request = urllib.request.Request(url, headers={"User-Agent": "yt-music-ctl/1"})
        with urllib.request.urlopen(request, timeout=8) as response:
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

        -f bestaudio/best --no-playlist --no-progress -o <tmpl> <url>

    There is deliberately no `-x`/`--audio-format`: bestaudio for YouTube is a
    single already-compressed stream (opus-in-webm or m4a), which mpv plays
    natively through the same container it would have streamed. Skipping the
    extract/convert step removes an ffmpeg dependency, a transcode, and a whole
    class of partial-output failures, at the cost of a `.webm`/`.m4a` extension
    instead of a fixed one (the file is renamed to `<videoId>.<ext>` either
    way). Output goes to a private temp directory inside the cache so a failed
    or interrupted run can never leave a partial file behind.
    """
    if not valid_video_id(video_id):
        return {"ok": False, "error": "Invalid video ID", "cached": False, "path": ""}
    tmp_dir = None
    try:
        os.makedirs(AUDIO_CACHE_DIR, mode=0o700, exist_ok=True)
        try:
            os.chmod(AUDIO_CACHE_DIR, 0o700)
        except OSError:
            pass
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
             "--no-progress", "-o", template, url],
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
    mpv_kill()
    urls = [f"https://music.youtube.com/watch?v={t['videoId']}" for t in track_list]
    ensure_private_runtime_dir()
    proc = subprocess.Popen(["mpv", "--no-video", "--really-quiet",
                             f"--input-ipc-server={MPV_SOCKET}",
                             "--keep-open=no"] + urls,
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
    ytm = get_ytmusic(require_auth=False)
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
        pl = ytm.get_playlist(playlist_id, limit=100)
        tracks = pl.get("tracks") or []
        urls = []
        meta = []
        for t in tracks:
            vid = t.get("videoId", "")
            if vid:
                urls.append(f"https://music.youtube.com/watch?v={vid}")
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
        ensure_private_runtime_dir()
        proc = subprocess.Popen(["mpv", "--no-video", "--really-quiet",
                                 f"--input-ipc-server={MPV_SOCKET}",
                                 "--keep-open=no"] + urls,
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
            data = ytm.get_playlist(target_id, limit=100)
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
        urls = [f"https://music.youtube.com/watch?v={t['videoId']}" for t in meta]
        if mode == "play" or not mpv_is_running():
            mpv_kill()
            ensure_private_runtime_dir()
            proc = subprocess.Popen(["mpv", "--no-video", "--really-quiet",
                                     f"--input-ipc-server={MPV_SOCKET}",
                                     "--keep-open=no"] + urls,
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
        urls = [f"https://music.youtube.com/watch?v={t['videoId']}" for t in meta]
        if mode == "play" or not mpv_is_running():
            mpv_kill()
            ensure_private_runtime_dir()
            proc = subprocess.Popen(["mpv", "--no-video", "--really-quiet",
                                     f"--input-ipc-server={MPV_SOCKET}",
                                     "--keep-open=no"] + urls,
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


def cmd_queue_list(args):
    empty = {"ok": True, "playing": False, "position": -1, "count": 0,
             "tracks": []}
    if not mpv_is_running():
        print(json.dumps(empty))
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
    tracks = []
    for index, entry in enumerate(playlist):
        if not isinstance(entry, dict):
            continue
        video_id = video_id_from_url(entry.get("filename") or "")
        if not valid_video_id(video_id):
            video_id = ""
        info = meta.get(video_id) if video_id else None
        info = info if isinstance(info, dict) else {}
        tracks.append({
            "index": index,
            "videoId": video_id,
            "title": str(info.get("title") or entry.get("title") or ""),
            "artist": str(info.get("artist") or ""),
            "album": str(info.get("album") or ""),
            "duration": info.get("duration") or 0,
            "current": bool(entry.get("current")),
        })
    print(json.dumps({"ok": True, "playing": True, "position": position,
                      "count": len(tracks), "tracks": tracks}))


def cmd_queue_jump(args):
    if not mpv_is_running():
        fail("Nothing playing")
    index = _queue_index(args, "Usage: yt-music-ctl queue-jump <index>")
    count = _queue_count()
    if count is None:
        fail("Unable to read playlist")
    # An out-of-range playlist-pos makes mpv exit, so validate first.
    if not 0 <= index < count:
        fail("Index out of range")
    mpv_send("set_property", ["playlist-pos", index])
    write_status_from_mpv(get_mpv_props())
    print(json.dumps({"ok": True, "position": index}))


def cmd_queue_remove(args):
    if not mpv_is_running():
        fail("Nothing playing")
    index = _queue_index(args, "Usage: yt-music-ctl queue-remove <index>")
    count = _queue_count()
    if count is None:
        fail("Unable to read playlist")
    if not 0 <= index < count:
        fail("Index out of range")
    mpv_send("playlist-remove", index)
    if mpv_is_running():
        write_status_from_mpv(get_mpv_props())
    else:
        write_status({"ok": True, "playing": False})
    print(json.dumps({"ok": True, "removed": index}))


def cmd_queue_move(args):
    if not mpv_is_running():
        fail("Nothing playing")
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
        fail("Index out of range")
    # mpv moves an entry to *take the place of* the entry at the target
    # index, so the entry lands one slot earlier when it moves forward.
    # Shifting the target forward (count == append) makes the entry finish
    # exactly at `to`, which is what the user asked for.
    target = to + 1 if frm < to else to
    mpv_send("playlist-move", [frm, target])
    print(json.dumps({"ok": True, "from": frm, "to": to}))


def cmd_stop(args):
    mpv_kill()
    write_status({"ok": True, "playing": False})
    print(json.dumps({"ok": True}))


def cmd_seek_pct(args):
    if not mpv_is_running():
        fail("Nothing playing")
    if not args:
        fail("Usage: yt-music-ctl seek-pct <0-100>")
    pct = max(0, min(100, float(args[0])))
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
    vol = max(0, min(150, int(args[0])))
    mpv_send("set_property", ["volume", vol])
    props = get_mpv_props()
    write_status_from_mpv(props)
    print(json.dumps({"ok": True, "volume": vol}))


def cmd_loop(args):
    if not mpv_is_running():
        print(json.dumps({"ok": False, "error": "Nothing playing"}))
        return
    mode = args[0] if args else "inf"
    mpv_send("set_property", ["loop-playlist", mode])
    props = get_mpv_props()
    if props:
        write_status_from_mpv(props)
    print(json.dumps({"ok": True, "loop": mode}))


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


def ensure_daemon():
    """Start the detached status daemon when it is missing or stale."""
    try:
        script_mtime = os.path.getmtime(os.path.realpath(__file__))
        if daemon_is_running():
            record = load_daemon_pid() or {}
            if record.get("script_mtime") == script_mtime:
                return False
            daemon_stop()
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
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
    except Exception:
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
            except Exception:
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
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
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
    "status": cmd_status,
    "play": cmd_play,
    "play-next": cmd_play_next,
    "queue-add": cmd_queue_add,
    "playlist-add": cmd_playlist_add,
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
    "artist": cmd_artist,
    "radio": cmd_artist_radio,
    "home": cmd_home,
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
    "queue-jump": cmd_queue_jump,
    "queue-remove": cmd_queue_remove,
    "queue-move": cmd_queue_move,
    "loop": cmd_loop,
    "shuffle": cmd_shuffle,
    "daemon": cmd_daemon,
    "watch": cmd_daemon,
    "ensure-daemon": cmd_ensure_daemon,
    "daemon-stop": cmd_daemon_stop,
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
        print("  like <videoId>           Like + add to Liked Music playlist")
        print("  dislike <videoId>        Remove like")
        print("  unlike <videoId>         Remove like (same as dislike)")
        print("  playlists                List library playlists")
        print("  create-playlist <name>   Create a private playlist")
        print("  playlist <playlistId>    Get playlist tracks")
        print("  playlist-add <id> <vid>  Add a track to a playlist (LM = liked)")
        print("  search [-f kind] <query>  Search songs|albums|artists|playlists")
        print("  liked [limit]            List liked songs")
        print("  library <kind> [limit]   List library songs|albums|artists|playlists")
        print("  home [sections]          Fetch the home feed (default 3 sections)")
        print("  history [limit]          List recently played tracks")
        print("  last-played [limit|clear] Local play history")
        print("  restore                  Rebuild the last queue, paused")
        print("  album <browseId>         Get album tracks")
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
        print("  queue-jump <index>       Jump to a queue index")
        print("  queue-remove <index>     Remove a queue entry")
        print("  queue-move <from> <to>   Move a queue entry")
        print("  loop <mode>              Set loop mode (off/inf)")
        print("  shuffle                  Shuffle current playlist")
        print("  daemon                   Run the status daemon in the foreground")
        print("  ensure-daemon            Start the status daemon in the background")
        print("  daemon-stop              Stop the status daemon")
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
