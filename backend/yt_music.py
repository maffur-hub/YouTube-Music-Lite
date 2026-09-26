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

def get_mpv_props():
    if not mpv_is_running():
        return None
    if not private_mpv_socket():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(MPV_SOCKET)
            props = {}
            for name in ["pause", "media-title", "metadata/by-key/artist",
                         "metadata/by-key/album", "duration", "time-pos",
                         "volume", "path", "filename"]:
                cmd = json.dumps({"command": ["get_property", name]}) + "\n"
                sock.sendall(cmd.encode())
                resp = json.loads(sock.recv(4096).decode().strip().split("\n")[0])
                props[name] = resp.get("data")
            return props
    except Exception:
        return None


def extract_video_id(props):
    if not props:
        return None
    path = props.get("path", "") or props.get("filename", "")
    if "v=" in path:
        for part in path.split("?"):
            if "v=" in part:
                return part.split("v=")[1].split("&")[0]
    if "youtu.be/" in path:
        return path.split("youtu.be/")[1].split("?")[0]
    if "youtube.com/watch" in path:
        import urllib.parse
        parsed = urllib.parse.urlparse(path)
        qs = urllib.parse.parse_qs(parsed.query)
        return qs.get("v", [None])[0]
    return None


def write_status_from_mpv(props):
    if not props:
        write_status({"ok": True, "playing": False})
        return
    paused = props.get("pause", True)
    title = props.get("media-title", "")
    artist = props.get("metadata/by-key/artist", "")
    album = props.get("metadata/by-key/album", "")
    duration = props.get("duration", 0) or 0
    position = props.get("time-pos", 0) or 0
    volume = props.get("volume", 100)
    video_id = extract_video_id(props)
    write_status({
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
    })


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


def cmd_play(args):
    if not args:
        fail("Usage: yt-music-ctl play <videoId>")
    video_id = args[0]
    mpv_play(video_id)
    props = get_mpv_props()
    write_status_from_mpv(props)
    print(json.dumps({"ok": True, "videoId": video_id}))


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


def cmd_search(args):
    if not args:
        fail("Usage: yt-music-ctl search <query>")
    query = " ".join(args)
    ytm = get_ytmusic(require_auth=False)
    try:
        results = ytm.search(query, filter="songs", limit=20)
        songs = []
        for r in results:
            if r.get("resultType") != "song":
                continue
            vid = r.get("videoId", "")
            if not vid:
                continue
            songs.append({
                "videoId": vid,
                "title": r.get("title", ""),
                "artist": ", ".join(a.get("name", "") for a in (r.get("artists") or [])),
                "album": r.get("album", {}).get("name", "") if r.get("album") else "",
                "duration": r.get("duration_seconds", 0) or 0,
                "thumbnail": (r.get("thumbnails", [{}])[-1].get("url", "")
                              if r.get("thumbnails") else ""),
            })
        print(json.dumps({"ok": True, "query": query, "songs": songs}))
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
        mpv_kill()
        urls = [f"https://music.youtube.com/watch?v={t['videoId']}" for t in tracks]
        ensure_private_runtime_dir()
        proc = subprocess.Popen(["mpv", "--no-video", "--really-quiet",
                                 f"--input-ipc-server={MPV_SOCKET}",
                                 "--keep-open=no"] + urls,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        json_dump(MPV_PID_PATH, mpv_pid_record(proc))
        wait_for_mpv()
        props = get_mpv_props()
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
    if not args:
        fail("Usage: yt-music-ctl queue <playlistId>")
    playlist_id = args[0]
    ytm = get_ytmusic()
    try:
        pl = ytm.get_playlist(playlist_id, limit=100)
        tracks = pl.get("tracks") or []
        urls = []
        for t in tracks:
            vid = t.get("videoId", "")
            if vid:
                urls.append(f"https://music.youtube.com/watch?v={vid}")
        if not urls:
            print(json.dumps({"ok": False, "error": "Empty playlist"}))
            return
        mpv_kill()
        ensure_private_runtime_dir()
        proc = subprocess.Popen(["mpv", "--no-video", "--really-quiet",
                                 f"--input-ipc-server={MPV_SOCKET}",
                                 "--keep-open=no"] + urls,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        json_dump(MPV_PID_PATH, mpv_pid_record(proc))
        wait_for_mpv()
        props = get_mpv_props()
        write_status_from_mpv(props)
        print(json.dumps({
            "ok": True,
            "queued": True,
            "title": pl.get("title", ""),
            "tracks": len(urls)
        }))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


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
        return
    mode = args[0] if args else "inf"
    mpv_send("set_property", ["loop-playlist", mode])


def cmd_shuffle(args):
    if not mpv_is_running():
        return
    mpv_send("playlist-shuffle")


# ---------------------------------------------------------------- watcher daemon

def cmd_watch(args):
    """Continuous status watcher - runs as a background process."""
    write_status({"ok": True, "playing": False, "watcher": True})

    while True:
        try:
            if not mpv_is_running():
                time.sleep(0.5)
                write_status({"ok": True, "playing": False, "watcher": True})
                continue
            props = get_mpv_props()
            if not props:
                write_status({"ok": True, "playing": False, "watcher": True})
                time.sleep(0.5)
                continue
            write_status_from_mpv(props)
            time.sleep(1)
        except Exception:
            time.sleep(1)


# ---------------------------------------------------------------- main

COMMANDS = {
    "login": cmd_login,
    "status": cmd_status,
    "play": cmd_play,
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
    "thumbnail": cmd_thumbnail,
    "mix": cmd_mix,
    "queue": cmd_queue_playlist,
    "loop": cmd_loop,
    "shuffle": cmd_shuffle,
    "watch": cmd_watch,
}


def main():
    if len(sys.argv) < 2:
        print("yt-music-ctl — YouTube Music bar widget backend")
        print()
        print("Commands:")
        print("  login                    Log in via browser")
        print("  status                   Update status from mpv")
        print("  play <videoId>           Play a song")
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
        print("  search <query>           Search for songs")
        print("  thumbnail <videoId>     Fetch a bounded album thumbnail")
        print("  mix <videoId>            Play radio mix from seed")
        print("  queue <playlistId>       Queue and play a playlist")
        print("  loop <mode>              Set loop mode (off/inf)")
        print("  shuffle                  Shuffle current playlist")
        print("  watch                    Run background status watcher")
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
