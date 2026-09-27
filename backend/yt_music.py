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
MAX_THUMBNAIL_BYTES = 1024 * 1024
MAX_THUMBNAIL_DIMENSION = 4096
MAX_THUMBNAIL_PIXELS = 16 * 1024 * 1024


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
        for key in ("title", "artist", "duration"):
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
             "playlist-pos", "playlist-count"]
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


def write_status_from_mpv(props, notify=True):
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
        "playlistPos": props.get("playlist-pos"),
        "playlistCount": props.get("playlist-count"),
    }
    if notify:
        notify_track_change(previous, status)
    write_status(status)
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
    write_status_from_mpv(props)
    title = (props or {}).get("media-title") or ""
    if _looks_like_url_title(title):
        title = ""
    remember_tracks([{
        "videoId": video_id,
        "title": title,
        "artist": (props or {}).get("metadata/by-key/artist") or "",
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
    if not args:
        fail("Usage: yt-music-ctl playlist <playlistId>")
    playlist_id = args[0]
    ytm = get_ytmusic()
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
        print(json.dumps({
            "ok": True,
            "title": pl.get("title", ""),
            "playlistId": playlist_id,
            "tracks": tracks
        }))
    except Exception as e:
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
    limit = 100
    if args:
        try:
            limit = max(1, min(300, int(args[0])))
        except ValueError:
            fail("Usage: yt-music-ctl liked [limit]")
    ytm = get_ytmusic()
    try:
        liked = ytm.get_liked_songs(limit=limit)
        items = [song_row(t) for t in (liked.get("tracks") or []) if t.get("videoId")]
        print(json.dumps({
            "ok": True,
            "title": liked.get("title", "Liked Music"),
            "playlistId": "LM",
            "items": items,
        }))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_library(args):
    if not args or args[0] not in ("songs", "albums", "artists", "playlists"):
        fail("Usage: yt-music-ctl library <songs|albums|artists|playlists> [limit]")
    kind = args[0]
    limit = 100
    if len(args) > 1:
        try:
            limit = max(1, min(500, int(args[1])))
        except ValueError:
            fail("Invalid limit")
    ytm = get_ytmusic()
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
        print(json.dumps({"ok": True, "kind": kind, "items": items}))
    except Exception as e:
        print(json.dumps({"ok": False, "kind": kind, "error": str(e)}))


def cmd_album(args):
    if not args:
        fail("Usage: yt-music-ctl album <browseId>")
    browse_id = args[0]
    ytm = get_ytmusic()
    try:
        album = ytm.get_album(browse_id)
        print(json.dumps({
            "ok": True,
            "browseId": browse_id,
            "title": album.get("title", ""),
            "artist": ", ".join(a.get("name", "") for a in (album.get("artists") or [])),
            "year": album.get("year", "") or "",
            "audioPlaylistId": album.get("audioPlaylistId", ""),
            "items": [song_row(t) for t in (album.get("tracks") or []) if t.get("videoId")],
        }))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_artist(args):
    if not args:
        fail("Usage: yt-music-ctl artist <browseId>")
    browse_id = args[0]
    ytm = get_ytmusic()
    try:
        artist = ytm.get_artist(browse_id)
        songs = ((artist.get("songs") or {}).get("results") or [])
        albums = ((artist.get("albums") or {}).get("results") or [])
        singles = ((artist.get("singles") or {}).get("results") or [])
        print(json.dumps({
            "ok": True,
            "browseId": browse_id,
            "name": artist.get("name", ""),
            "subscribers": artist.get("subscribers", "") or "",
            "description": artist.get("description", "") or "",
            "items": ([song_row(t) for t in songs if t.get("videoId")]
                      + [library_album_row(a) for a in (albums + singles) if a.get("browseId")]),
        }))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_search(args):
    search_filters = ("songs", "albums", "artists", "playlists")
    if args and args[0] == "-f":
        if len(args) < 2 or args[1] not in search_filters:
            fail("Invalid search filter")
        filter_kind = args[1]
        query = " ".join(args[2:])
    else:
        filter_kind = "songs"
        query = " ".join(args)
    if not query.strip():
        fail("Usage: yt-music-ctl search [-f songs|albums|artists|playlists] <query>")
    expected = {"songs": "song", "albums": "album",
                "artists": "artist", "playlists": "playlist"}[filter_kind]
    ytm = get_ytmusic(require_auth=False)
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
        print(json.dumps({"ok": True, "query": query, "filter": filter_kind,
                          "items": items}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_home(args):
    sections = 3
    if args:
        try:
            sections = max(1, min(8, int(args[0])))
        except ValueError:
            fail("Usage: yt-music-ctl home [sections]")
    ytm = get_ytmusic()
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
        print(json.dumps({"ok": True, "items": items}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


def cmd_history(args):
    limit = 100
    if args:
        try:
            limit = max(1, min(300, int(args[0])))
        except ValueError:
            fail("Usage: yt-music-ctl history [limit]")
    ytm = get_ytmusic()
    try:
        rows = ytm.get_history() or []
        items = [song_row(t) for t in rows[:limit] if t.get("videoId")]
        print(json.dumps({"ok": True, "items": items}))
    except Exception as e:
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


def cmd_mix(args):
    ensure_daemon()
    if not args:
        fail("Usage: yt-music-ctl mix <videoId> [playlistId]")
    seed_id = args[0]
    ytm = get_ytmusic(require_auth=False)
    try:
        watchlist = ytm.get_watch_playlist(seed_id, limit=50)
        tracks = []
        for track in (watchlist.get("tracks") or []):
            vid = track.get("videoId", "")
            if not vid:
                continue
            tracks.append({
                "videoId": vid,
                "title": track.get("title", ""),
                "artist": ", ".join(a.get("name", "") for a in (track.get("artists") or [])),
                "duration": track.get("duration_seconds", 0) or 0,
            })
        if not tracks:
            print(json.dumps({"ok": False, "error": "No mix tracks found"}))
            return
        remember_tracks(tracks)
        mpv_kill()
        urls = [f"https://music.youtube.com/watch?v={t['videoId']}" for t in tracks]
        ensure_private_runtime_dir()
        proc = subprocess.Popen(["mpv", "--no-video", "--really-quiet",
                                 f"--input-ipc-server={MPV_SOCKET}",
                                 "--keep-open=no"] + urls,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        json_dump(MPV_PID_PATH, mpv_pid_record(proc))
        wait_for_mpv()
        props = wait_for_metadata()
        write_status_from_mpv(props)
        print(json.dumps({
            "ok": True,
            "mix": True,
            "seedId": seed_id,
            "tracks": len(tracks)
        }))
    except Exception as e:
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
                meta.append({
                    "videoId": vid,
                    "title": t.get("title", ""),
                    "artist": ", ".join(a.get("name", "")
                                        for a in (t.get("artists") or [])),
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
        write_status_from_mpv(props)
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
            meta.append({
                "videoId": vid,
                "title": t.get("title", ""),
                "artist": ", ".join(a.get("name", "")
                                    for a in (t.get("artists") or [])),
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
            write_status_from_mpv(props)
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
            write_status_from_mpv(props)
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
        return
    mpv_send("playlist-shuffle")


# ---------------------------------------------------------------- status daemon

OBSERVED_PROPERTIES = [
    "pause", "media-title", "metadata/by-key/artist", "metadata/by-key/album",
    "duration", "time-pos", "volume", "path", "loop-playlist",
    "playlist-pos", "playlist-count",
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
    # Snapshot of the last status that is safe to compare against: the ungated
    # status write would otherwise put the new (videoId, title) into the file
    # during a hold and make notify_track_change's backstop swallow the
    # deferred notification.
    notify_previous = read_status()

    def flush():
        nonlocal last_signature, notify_key, notify_previous
        props = dict(cache)
        for key, default in (("pause", True), ("media-title", ""), ("path", ""),
                             ("volume", 100), ("duration", 0), ("time-pos", 0)):
            if props.get(key) is None:
                props[key] = default
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
    "home": cmd_home,
    "history": cmd_history,
    "enqueue": cmd_enqueue,
    "enqueue-files": cmd_enqueue_files,
    "thumbnail": cmd_thumbnail,
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
        print("  album <browseId>         Get album tracks")
        print("  artist <browseId>        Get an artist's top songs + albums")
        print("  enqueue <play|queue|next> <album|artist|playlist> <id>   Play/queue a whole album, artist or playlist")
        print("  enqueue-files <play|queue|next> <videoId...>   Play/queue an explicit list of videoIds")
        print("  thumbnail <videoId>     Fetch a bounded album thumbnail")
        print("  mix <videoId>            Play radio mix from seed")
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
