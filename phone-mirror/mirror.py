#!/usr/bin/env python3
"""
Phone Mirror - see and control an Android phone from your laptop over USB.

Nothing is installed on the phone. This script only talks to `adb` on the
laptop, which uses tools that already ship with Android:
  * screencap / screenrecord  -> to read the screen
  * input                     -> to tap, swipe, type and press buttons

Usage:
    python mirror.py                 # mirror the first allowed phone
    python mirror.py --fast          # smoother video (needs ffmpeg on the laptop)
    python mirror.py --list          # show connected phones and their serials
    python mirror.py --allow SERIAL  # remember a phone as "mine"

Then open http://127.0.0.1:8765 in your browser.

Only Python 3.8+ standard library is used - no pip installs needed.
"""

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ALLOW_FILE = HERE / "allowed_devices.txt"
BOUNDARY = "phonemirrorframe"

# Android key codes used by the on-screen buttons and keyboard forwarding.
KEYCODES = {
    "home": 3, "back": 4, "call": 5, "endcall": 6,
    "up": 19, "down": 20, "left": 21, "right": 22, "center": 23,
    "volup": 24, "voldown": 25, "power": 26, "camera": 27,
    "tab": 61, "space": 62, "enter": 66, "del": 67, "menu": 82,
    "escape": 111, "forward_del": 112, "move_home": 122, "move_end": 123,
    "recents": 187, "wakeup": 224, "sleep": 223, "mute": 164,
}


# --------------------------------------------------------------------------
# adb helpers
# --------------------------------------------------------------------------

def find_adb(explicit=None):
    candidates = [explicit] if explicit else []
    candidates += [shutil.which("adb")]
    # Common SDK / platform-tools locations.
    home = Path.home()
    candidates += [
        str(HERE / "platform-tools" / "adb"),
        str(HERE / "platform-tools" / "adb.exe"),
        str(home / "platform-tools" / "adb"),
        str(home / "platform-tools" / "adb.exe"),
        str(home / "Android" / "Sdk" / "platform-tools" / "adb"),
        str(home / "Library" / "Android" / "sdk" / "platform-tools" / "adb"),
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def list_devices(adb):
    """Return list of (serial, state, description)."""
    out = subprocess.run([adb, "devices", "-l"], capture_output=True, text=True).stdout
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1], " ".join(parts[2:])))
    return devices


def load_allowlist(extra):
    allowed = set(extra or [])
    if ALLOW_FILE.is_file():
        for line in ALLOW_FILE.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                allowed.add(line)
    return allowed


def save_allowlist(serials):
    existing = load_allowlist([])
    new = [s for s in serials if s not in existing]
    if new:
        with ALLOW_FILE.open("a") as f:
            for s in new:
                f.write(s + "\n")
    return new


def shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def png_size(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    return None


class Phone:
    def __init__(self, adb, serial):
        self.adb = adb
        self.serial = serial
        self.lock = threading.Lock()  # serialise input commands so taps stay in order

    def cmd(self, *args):
        return [self.adb, "-s", self.serial, *args]

    def run(self, *args, timeout=15):
        return subprocess.run(self.cmd(*args), capture_output=True, timeout=timeout)

    def shell(self, command, timeout=15):
        return self.run("shell", command, timeout=timeout)

    def screencap(self):
        r = self.run("exec-out", "screencap", "-p", timeout=20)
        data = r.stdout
        if not data.startswith(b"\x89PNG"):
            # Very old adb/Android without exec-out: fall back to shell + CRLF fix.
            r = self.run("shell", "screencap", "-p", timeout=20)
            data = r.stdout.replace(b"\r\n", b"\n")
        return data if data.startswith(b"\x89PNG") else None

    def screen_size(self):
        out = self.shell("wm size").stdout.decode(errors="ignore")
        size = None
        for line in out.splitlines():
            if ":" in line and "x" in line:
                try:
                    w, h = line.split(":", 1)[1].strip().split("x")
                    size = (int(w), int(h))  # "Override size" (listed last) wins
                except ValueError:
                    pass
        return size

    def input(self, command):
        with self.lock:
            self.shell("input " + command)

    def info(self):
        model = self.shell("getprop ro.product.model").stdout.decode(errors="ignore").strip()
        release = self.shell("getprop ro.build.version.release").stdout.decode(errors="ignore").strip()
        return {"serial": self.serial, "model": model, "android": release}


# --------------------------------------------------------------------------
# Frame sources - produce the latest screen image for the browser
# --------------------------------------------------------------------------

class FrameSource:
    content_type = "image/png"

    def __init__(self, phone):
        self.phone = phone
        self.cond = threading.Condition()
        self.frame = None
        self.frame_id = 0
        self.size = None  # (w, h) of the phone screen in the current orientation
        self.clients = 0
        self.error = None
        self.fps = 0.0

    def publish(self, data):
        with self.cond:
            self.frame = data
            self.frame_id += 1
            self.cond.notify_all()

    def wait_frame(self, last_id, timeout=5):
        with self.cond:
            self.cond.wait_for(lambda: self.frame_id != last_id, timeout=timeout)
            return self.frame_id, self.frame

    def add_client(self, delta):
        with self.cond:
            self.clients += delta
            self.cond.notify_all()

    def wait_for_clients(self):
        with self.cond:
            self.cond.wait_for(lambda: self.clients > 0)

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()


class ScreencapSource(FrameSource):
    """Grabs PNG screenshots in a loop. Works everywhere, ~2-6 fps."""

    def loop(self):
        times = []
        while True:
            self.wait_for_clients()  # don't drain the battery when nobody is watching
            try:
                data = self.phone.screencap()
            except subprocess.TimeoutExpired:
                data = None
            if not data:
                self.error = "Could not read the screen. Is the phone connected and authorised?"
                time.sleep(1)
                continue
            self.error = None
            self.size = png_size(data) or self.size
            now = time.time()
            times = [t for t in times if now - t < 3] + [now]
            self.fps = (len(times) - 1) / max(now - times[0], 0.001)
            self.publish(data)


class ScreenrecordSource(FrameSource):
    """Streams H.264 from the phone's built-in screenrecord, decoded by ffmpeg.

    Much smoother (~20-60 fps) but needs ffmpeg on the laptop.
    """

    content_type = "image/jpeg"

    def __init__(self, phone, ffmpeg, max_size, bitrate):
        super().__init__(phone)
        self.ffmpeg = ffmpeg
        self.max_size = max_size
        self.bitrate = bitrate

    def loop(self):
        while True:
            self.wait_for_clients()
            self.size = self.phone.screen_size() or self.size
            rec_args = ["exec-out", "screenrecord", "--output-format=h264",
                        "--bit-rate", str(self.bitrate)]
            if self.size and self.max_size:
                w, h = self.size
                scale = min(1.0, self.max_size / max(w, h))
                # Encoder wants even dimensions.
                rec_args += ["--size", f"{int(w * scale) // 2 * 2}x{int(h * scale) // 2 * 2}"]
            rec_args.append("-")
            rec = subprocess.Popen(self.phone.cmd(*rec_args), stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
            dec = subprocess.Popen(
                [self.ffmpeg, "-loglevel", "error", "-flags", "low_delay",
                 "-probesize", "32", "-analyzeduration", "0",
                 "-f", "h264", "-i", "pipe:0", "-vsync", "passthrough",
                 "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "pipe:1"],
                stdin=rec.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            rec.stdout.close()
            started = time.time()
            got_frame = self._pump(dec.stdout)
            for p in (dec, rec):
                try:
                    p.kill()
                except OSError:
                    pass
                p.wait()
            if not got_frame:
                self.error = ("screenrecord produced no video. Try running without --fast. "
                              "(Some phones block screenrecord.)")
                time.sleep(2)
            elif time.time() - started < 2:
                time.sleep(1)
            # screenrecord stops by itself after 3 minutes; loop restarts it.

    def _pump(self, stream):
        buf = b""
        got = False
        times = []
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not chunk:
                return got
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    buf = buf[-1:]
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end < 0:
                    buf = buf[start:]
                    break
                frame = buf[start:end + 2]
                buf = buf[end + 2:]
                got = True
                self.error = None
                now = time.time()
                times = [t for t in times if now - t < 3] + [now]
                self.fps = (len(times) - 1) / max(now - times[0], 0.001)
                self.publish(frame)
            if self.clients == 0:
                return got


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    phone = None
    source = None
    info = {}

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/stream":
            self.stream()
        elif path == "/status":
            s = self.source
            self._send(200, {**self.info, "size": s.size, "fps": round(s.fps, 1),
                             "error": s.error, "mode": type(s).__name__})
        elif path == "/screenshot":
            data = self.phone.screencap()
            if data:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="phone-{time.strftime("%Y%m%d-%H%M%S")}.png"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(503, {"error": "screenshot failed"})
        else:
            self._send(404, {"error": "not found"})

    def stream(self):
        src = self.source
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        src.add_client(1)
        last = -1
        try:
            while True:
                fid, frame = src.wait_frame(last)
                if fid == last or frame is None:
                    continue
                last = fid
                self.wfile.write(
                    f"--{BOUNDARY}\r\nContent-Type: {src.content_type}\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n".encode() + frame + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            src.add_client(-1)

    def do_POST(self):
        if urlparse(self.path).path != "/input":
            return self._send(404, {"error": "not found"})
        # Only accept requests from our own page (blocks other websites poking the phone).
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and urlparse(origin).netloc != host:
            return self._send(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            ev = json.loads(self.rfile.read(length) or b"{}")
            self.handle_input(ev)
            self._send(200, {"ok": True})
        except Exception as e:  # report back to the page instead of crashing
            self._send(400, {"error": str(e)})

    def handle_input(self, ev):
        kind = ev.get("type")
        size = self.source.size or self.phone.screen_size()
        if kind in ("tap", "swipe", "longpress") and not size:
            raise ValueError("screen size unknown yet")

        def px(fx, fy):
            fx = min(max(float(fx), 0.0), 1.0)
            fy = min(max(float(fy), 0.0), 1.0)
            return round(fx * (size[0] - 1)), round(fy * (size[1] - 1))

        if kind == "tap":
            x, y = px(ev["x"], ev["y"])
            cmd = f"tap {x} {y}"
        elif kind in ("swipe", "longpress"):
            x1, y1 = px(ev["x1"], ev["y1"])
            x2, y2 = px(ev.get("x2", ev["x1"]), ev.get("y2", ev["y1"]))
            ms = int(min(max(int(ev.get("ms", 300)), 1), 10000))
            cmd = f"swipe {x1} {y1} {x2} {y2} {ms}"
        elif kind == "key":
            key = str(ev["key"]).lower()
            code = KEYCODES.get(key)
            if code is None:
                raise ValueError(f"unknown key {key}")
            cmd = f"keyevent {code}"
        elif kind == "text":
            text = str(ev["text"])
            if not text:
                return
            # `input text` uses %s for spaces.
            cmd = "text " + shell_quote(text.replace(" ", "%s"))
        else:
            raise ValueError(f"unknown input type {kind}")
        threading.Thread(target=self.phone.input, args=(cmd,), daemon=True).start()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def pick_device(adb, args):
    devices = list_devices(adb)
    if args.allow:
        added = save_allowlist(args.allow)
        if added:
            print(f"Saved to {ALLOW_FILE.name}: {', '.join(added)}")
    allowed = load_allowlist([])

    if args.list:
        if not devices:
            print("No phones found.")
        for serial, state, desc in devices:
            mark = "  (allowed)" if serial in allowed else ""
            print(f"{serial:24} {state:14} {desc}{mark}")
        sys.exit(0)

    if not devices:
        sys.exit("No phone found. Plug it in with a data USB cable and check README.md "
                 "(USB debugging must already be on).")

    for serial, state, _ in devices:
        if state == "unauthorized":
            print(f"! {serial} is 'unauthorized': the phone is waiting for you to tap "
                  "'Allow USB debugging' on its screen. See README.md -> 'Broken screen'.")

    ready = [d for d in devices if d[1] == "device"]
    if allowed:
        ready = [d for d in ready if d[0] in allowed]
        if not ready:
            sys.exit("None of your allowed phones are connected and ready. "
                     "Run `python mirror.py --list` to see what is plugged in.")
    if args.serial:
        ready = [d for d in ready if d[0] == args.serial]
        if not ready:
            sys.exit(f"Phone {args.serial} is not connected/ready.")
    if not ready:
        sys.exit("A phone is connected but not ready (see messages above).")

    serial = ready[0][0]
    if not allowed:
        print(f"Tip: lock this tool to your phone with: python mirror.py --allow {serial}")
    return serial


def main():
    ap = argparse.ArgumentParser(description="Mirror and control an Android phone over USB.")
    ap.add_argument("--serial", help="use this phone (see --list)")
    ap.add_argument("--allow", action="append", metavar="SERIAL",
                    help="remember a phone as yours; only remembered phones will be used")
    ap.add_argument("--list", action="store_true", help="list connected phones and exit")
    ap.add_argument("--fast", action="store_true",
                    help="smooth video using screenrecord + ffmpeg")
    ap.add_argument("--max-size", type=int, default=1280,
                    help="--fast: longest side of the video in pixels (default 1280)")
    ap.add_argument("--bitrate", type=int, default=6_000_000, help="--fast: video bitrate")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--adb", help="path to adb if it is not on PATH")
    ap.add_argument("--ffmpeg", help="path to ffmpeg if it is not on PATH")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    args = ap.parse_args()

    adb = find_adb(args.adb)
    if not adb:
        sys.exit("adb not found. Download Android 'SDK Platform-Tools' "
                 "(https://developer.android.com/tools/releases/platform-tools), unzip it "
                 "next to this script as ./platform-tools, or pass --adb PATH.")

    subprocess.run([adb, "start-server"], capture_output=True)
    serial = pick_device(adb, args)
    phone = Phone(adb, serial)

    if args.fast:
        ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
        if not ffmpeg:
            sys.exit("--fast needs ffmpeg installed on the laptop (https://ffmpeg.org). "
                     "Or run without --fast.")
        source = ScreenrecordSource(phone, ffmpeg, args.max_size, args.bitrate)
    else:
        source = ScreencapSource(phone)
    source.start()

    Handler.phone = phone
    Handler.source = source
    Handler.info = phone.info()

    # 127.0.0.1 only: the phone can't be controlled from other computers on your network.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{args.port}"
    info = Handler.info
    print(f"Connected to {info.get('model') or serial} (Android {info.get('android')}, {serial})")
    print(f"Open {url} in your browser. Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
