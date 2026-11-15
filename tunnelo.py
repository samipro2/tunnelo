#!/usr/bin/env python3
"""
Tunnelo - put a locally hosted web app on the internet in one click.

Engine : Cloudflare quick tunnels (cloudflared) - free, no account, no signup.
GUI    : PyQt6
Extras : auto-downloads the tunnel engine, auto-reconnects (24/7), health checks,
         keeps Windows awake while tunneling, minimizes to the system tray.
"""
from __future__ import annotations

import ctypes
import math
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from PyQt6.QtCore import QRectF, QSettings, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Tunnelo"
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def popen_kwargs() -> dict:
    """Hide the console window on Windows."""
    return {"creationflags": 0x08000000} if IS_WIN else {}


def data_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def engine_path() -> Path:
    d = data_dir() / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d / ("cloudflared.exe" if IS_WIN else "cloudflared")


def engine_asset() -> str:
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "386" if m in ("i386", "i686", "x86") else "amd64"
    if IS_WIN:
        return f"cloudflared-windows-{'amd64' if arch == 'arm64' else arch}.exe"
    if IS_MAC:
        return f"cloudflared-darwin-{'amd64' if arch == '386' else arch}.tgz"
    return f"cloudflared-linux-{arch}"


def find_engine() -> str | None:
    own = engine_path()
    if own.exists():
        return str(own)
    return shutil.which("cloudflared")


def normalize_target(text: str) -> tuple[str, str, int]:
    """'8765' | '127.0.0.1:8765' | 'localhost:3000' | 'http://...' -> (url, host, port)."""
    t = text.strip()
    if not t:
        raise ValueError("Enter your local address, e.g. 127.0.0.1:8765")
    if re.fullmatch(r"\d{1,5}", t):
        t = f"127.0.0.1:{t}"
    if "://" not in t:
        t = "http://" + t
    try:
        u = urlparse(t)
        port = u.port
    except ValueError:
        raise ValueError("That address doesn't look right. Try 127.0.0.1:8765")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("That address doesn't look right. Try 127.0.0.1:8765")
    if port is None:
        port = 443 if u.scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    host = u.hostname
    if host == "0.0.0.0":
        host = "127.0.0.1"
    shown = f"[{host}]" if ":" in host else host
    return f"{u.scheme}://{shown}:{port}", host, port


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def keep_awake(on: bool) -> None:
    """Stop Windows from sleeping while the tunnel is running."""
    if IS_WIN:
        try:
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
        except Exception:
            pass


def make_icon(size: int = 256) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r = QRectF(8, 8, size - 16, size - 16)
    g = QLinearGradient(r.topLeft(), r.bottomRight())
    g.setColorAt(0, QColor("#6C5CE7"))
    g.setColorAt(1, QColor("#00C2FF"))
    path = QPainterPath()
    path.addRoundedRect(r, size * 0.24, size * 0.24)
    p.fillPath(path, g)
    pen = QPen(QColor("white"), size * 0.065)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    cx, cy = size / 2, size * 0.68
    for rad in (0.30, 0.205, 0.11):
        p.drawArc(QRectF(cx - size * rad, cy - size * rad, size * rad * 2, size * rad * 2), 0, 180 * 16)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("white"))
    p.drawEllipse(QRectF(cx - size * 0.035, cy - size * 0.035, size * 0.07, size * 0.07))
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------- #
#  Engine downloader
# --------------------------------------------------------------------------- #
class DownloadWorker(QThread):
    progress = pyqtSignal(int)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        asset = engine_asset()
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
        dest = engine_path()
        tmp = dest.with_name(dest.name + ".part")
        last_err = "unknown error"
        for attempt in range(3):
            if self._cancel:
                return
            try:
                req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/1.0"})
                with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
                    total = int(r.headers.get("Content-Length") or 0)
                    got = 0
                    while True:
                        if self._cancel:
                            return
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            self.progress.emit(int(got * 100 / total))
                if asset.endswith(".tgz"):
                    with tarfile.open(tmp) as tf:
                        member = next(m for m in tf.getmembers() if m.name.endswith("cloudflared"))
                        with tf.extractfile(member) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
                    tmp.unlink(missing_ok=True)
                else:
                    tmp.replace(dest)
                dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
                res = subprocess.run([str(dest), "--version"], capture_output=True, timeout=20, **popen_kwargs())
                if res.returncode != 0:
                    raise RuntimeError("downloaded engine failed to start")
                self.done.emit(str(dest))
                return
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                time.sleep(2)
        tmp.unlink(missing_ok=True)
        self.failed.emit(f"Couldn't download the tunnel engine ({last_err}). Check your internet connection and try again.")


# --------------------------------------------------------------------------- #
#  Tunnel worker: runs cloudflared, watches it, restarts it forever
# --------------------------------------------------------------------------- #
class TunnelWorker(QThread):
    state = pyqtSignal(str, str)      # key, message
    url_ready = pyqtSignal(str)
    log_line = pyqtSignal(str)
    reconnects = pyqtSignal(int)
    local_state = pyqtSignal(bool)

    CONNECT_TIMEOUT = 45
    HEALTH_INTERVAL = 30
    HEALTH_FAILS = 3

    def __init__(self, engine: str, target: str, host: str, port: int, parent=None):
        super().__init__(parent)
        self.engine, self.target, self.host, self.port = engine, target, host, port
        self._stop_evt = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # -- control --------------------------------------------------------- #
    def stop(self):
        self._stop_evt.set()
        self._kill()

    def _kill(self):
        with self._lock:
            p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    # -- watchers -------------------------------------------------------- #
    def _local_loop(self):
        last = None
        while not self._stop_evt.is_set():
            ok = port_open(self.host, self.port)
            if ok != last:
                self.local_state.emit(ok)
                last = ok
            self._stop_evt.wait(3)

    @staticmethod
    def _public_ok(url: str) -> bool:
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": f"{APP_NAME}/1.0"})
            urllib.request.urlopen(req, timeout=10).close()
            return True
        except urllib.error.HTTPError as e:
            return e.code != 530          # 530 = tunnel is gone; other codes mean the edge answered
        except Exception:
            return False

    # -- one tunnel session --------------------------------------------- #
    def _run_once(self, proto: str) -> bool:
        cfg = data_dir() / "empty-config.yml"
        if not cfg.exists():
            # Non-empty on purpose (cloudflared logs an error for an empty file). Also keeps any
            # user-level ~/.cloudflared/config.yml from interfering with quick tunnels.
            cfg.write_text("no-autoupdate: true\n")
        cmd = [self.engine, "tunnel", "--config", str(cfg), "--no-autoupdate",
               "--protocol", proto, "--url", self.target]
        if self.target.startswith("https://"):
            cmd.append("--no-tls-verify")
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1, **popen_kwargs(),
            )
        except OSError as e:
            self.log_line.emit(f"Could not start engine: {e}")
            return False
        with self._lock:
            self._proc = proc

        st = {"url": None, "url_t": 0.0, "registered": False, "live": False}

        def reader():
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip()
                if not line:
                    continue
                self.log_line.emit(line)
                if not st["url"]:
                    m = URL_RE.search(line)
                    if m and m.group(0).lower() != "https://api.trycloudflare.com":
                        st["url"], st["url_t"] = m.group(0), time.time()
                if "Registered tunnel connection" in line:
                    st["registered"] = True

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        t0 = time.time()
        next_check = 0.0
        healthy_once = False
        fails = 0
        while not self._stop_evt.is_set() and proc.poll() is None:
            now = time.time()
            if not st["live"]:
                if st["url"] and (st["registered"] or now - st["url_t"] >= 12):
                    st["live"] = True
                    next_check = now + 20
                    self.url_ready.emit(st["url"])
                    self.state.emit("live", "Live - your app is public")
                elif now - t0 > self.CONNECT_TIMEOUT:
                    self.log_line.emit("Timed out waiting for the tunnel; retrying...")
                    break
            elif now >= next_check:
                next_check = now + self.HEALTH_INTERVAL
                if self._public_ok(st["url"]):
                    healthy_once, fails = True, 0
                elif healthy_once:
                    fails += 1
                    self.log_line.emit(f"Health check failed ({fails}/{self.HEALTH_FAILS})")
                    if fails >= self.HEALTH_FAILS:
                        self.log_line.emit("Tunnel looks dead; restarting it...")
                        break
            self._stop_evt.wait(1)

        self._kill()
        t.join(2)
        return st["live"]

    # -- main loop ------------------------------------------------------- #
    def run(self):
        threading.Thread(target=self._local_loop, daemon=True).start()
        protocols = ["http2", "quic"]
        attempt = 0
        connect_failures = 0
        restarts = 0
        while not self._stop_evt.is_set():
            self.state.emit("connecting", "Connecting..." if attempt == 0 else "Reconnecting...")
            t0 = time.time()
            was_live = self._run_once(protocols[connect_failures % 2])
            if self._stop_evt.is_set():
                break
            lived = time.time() - t0
            if was_live:
                connect_failures = 0
                if lived > 60:
                    attempt = 0
            else:
                connect_failures += 1
            attempt += 1
            restarts += 1
            self.reconnects.emit(restarts)
            delay = 1 if (was_live and lived > 60) else min(2 ** min(attempt, 5), 30)
            self.state.emit("reconnecting", f"Connection lost - retrying in {delay}s...")
            self._stop_evt.wait(delay)


# --------------------------------------------------------------------------- #
#  UI widgets
# --------------------------------------------------------------------------- #
class StatusDot(QWidget):
    COLORS = {"idle": "#6B7599", "connecting": "#FFA502", "reconnecting": "#FFA502",
              "live": "#2ED573", "error": "#FF4757"}

    def __init__(self):
        super().__init__()
        self.setFixedSize(18, 18)
        self._state, self._phase = "idle", 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(50)

    def set_state(self, s: str):
        self._state = s
        self.update()

    def _step(self):
        if self._state in ("connecting", "reconnecting", "live"):
            self._phase += 0.07 if self._state == "live" else 0.18
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(self.COLORS.get(self._state, "#6B7599"))
        glow = QColor(c)
        pulsing = self._state in ("connecting", "reconnecting", "live")
        glow.setAlphaF(0.30 + 0.30 * math.sin(self._phase) if pulsing else 0.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QRectF(0, 0, 18, 18))
        p.setBrush(c)
        p.drawEllipse(QRectF(4, 4, 10, 10))


STYLE = """
* { font-family: "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif; color: #E6EAF7; }
QWidget#root { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #0A0F1E, stop:1 #151C3C); }
QFrame#card { background: #121A35; border: 1px solid #222B4F; border-radius: 16px; }
QLabel { background: transparent; }
QLabel#title { font-size: 26px; font-weight: 800; }
QLabel#subtitle, QLabel#muted { color: #8A94B8; font-size: 12px; }
QLabel#caption { color: #8A94B8; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; }
QLabel#status { font-size: 15px; font-weight: 700; }
QLabel#error { color: #FF6B7A; font-size: 12px; }
QLabel#url { font-size: 15px; font-weight: 700; color: #7FE0FF; }
QLineEdit { background: #0C1230; border: 1px solid #28325C; border-radius: 12px; padding: 0 16px;
            font-size: 17px; font-family: "Cascadia Mono", "Consolas", "Menlo", monospace; selection-background-color: #6C5CE7; }
QLineEdit:focus { border: 1px solid #7C6CFF; }
QLineEdit:disabled { color: #6B7599; background: #0E1430; }
QPushButton { border: none; border-radius: 12px; font-size: 14px; font-weight: 600; }
QPushButton#primary { color: white; font-size: 16px; font-weight: 800;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #6C5CE7, stop:1 #00A8E8); }
QPushButton#primary:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #7F70F5, stop:1 #22BBF5); }
QPushButton#primary:pressed { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #5849CC, stop:1 #0090C8); }
QPushButton#primary[running="true"] { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #E8384F, stop:1 #FF6B81); }
QPushButton#primary[running="true"]:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #F24E63, stop:1 #FF8497); }
QPushButton#secondary { background: #1C2650; color: #DCE3FF; }
QPushButton#secondary:hover { background: #26336A; }
QPushButton#link { background: transparent; color: #8A94B8; font-size: 12px; text-align: left; }
QPushButton#link:hover { color: #E6EAF7; }
QPlainTextEdit { background: #0A1028; border: 1px solid #222B4F; border-radius: 12px; padding: 8px; color: #9FB0E6;
    font-family: "Cascadia Mono", "Consolas", "Menlo", monospace; font-size: 11px; }
QMenu { background: #121A35; border: 1px solid #222B4F; padding: 6px; }
QMenu::item { padding: 6px 18px; border-radius: 6px; }
QMenu::item:selected { background: #26336A; }
"""


def card() -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(20, 18, 20, 18)
    lay.setSpacing(10)
    return f, lay


def label(text: str, name: str = "") -> QLabel:
    l = QLabel(text)
    if name:
        l.setObjectName(name)
    return l


# --------------------------------------------------------------------------- #
#  Main window
# --------------------------------------------------------------------------- #
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon())
        self.setMinimumSize(520, 590)
        self.resize(540, 640)

        self.settings = QSettings(APP_NAME, APP_NAME)
        self.worker: TunnelWorker | None = None
        self.dl: DownloadWorker | None = None
        self.url = ""
        self.started_at: float | None = None
        self.restart_count = 0
        self._quitting = False
        self._tray_hint = False

        self._build_ui()
        self._build_tray()

        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(1000)

    # ---- UI ------------------------------------------------------------ #
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 18)
        root.setSpacing(16)

        # header
        head = QHBoxLayout()
        head.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(make_icon(128).pixmap(52, 52))
        head.addWidget(logo)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(label(APP_NAME, "title"))
        col.addWidget(label("CO-app by sunsoft", "subtitle"))
        head.addLayout(col)
        head.addStretch()
        root.addLayout(head)

        # input card
        c1, l1 = card()
        l1.addWidget(label("LOCAL ADDRESS", "caption"))
        self.input = QLineEdit(self.settings.value("address", "127.0.0.1:8765"))
        self.input.setPlaceholderText("127.0.0.1:8765")
        self.input.setFixedHeight(50)
        self.input.returnPressed.connect(self._on_toggle)
        l1.addWidget(self.input)
        self.err = label("", "error")
        self.err.setWordWrap(True)
        self.err.hide()
        l1.addWidget(self.err)
        self.btn = QPushButton("Start Tunnel")
        self.btn.setObjectName("primary")
        self.btn.setProperty("running", "false")
        self.btn.setFixedHeight(54)
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.clicked.connect(self._on_toggle)
        l1.addWidget(self.btn)
        l1.addWidget(label("Works with  127.0.0.1:8765  ·  localhost:3000  ·  8080", "muted"))
        root.addWidget(c1)

        # status card
        c2, l2 = card()
        row = QHBoxLayout()
        row.setSpacing(10)
        self.dot = StatusDot()
        self.status = label("Ready to start", "status")
        self.uptime = label("", "muted")
        row.addWidget(self.dot)
        row.addWidget(self.status)
        row.addStretch()
        row.addWidget(self.uptime)
        l2.addLayout(row)
        self.local_chip = label("", "muted")
        self.local_chip.hide()
        l2.addWidget(self.local_chip)
        root.addWidget(c2)

        # url card
        self.url_card, l3 = card()
        l3.addWidget(label("PUBLIC URL", "caption"))
        self.url_label = label("", "url")
        self.url_label.setWordWrap(True)
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        l3.addWidget(self.url_label)
        r = QHBoxLayout()
        r.setSpacing(10)
        self.copy_btn = QPushButton("Copy link")
        self.open_btn = QPushButton("Open in browser")
        for b in (self.copy_btn, self.open_btn):
            b.setObjectName("secondary")
            b.setFixedHeight(42)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            r.addWidget(b)
        self.copy_btn.clicked.connect(self._copy)
        self.open_btn.clicked.connect(lambda: self.url and QDesktopServices.openUrl(QUrl(self.url)))
        l3.addLayout(r)
        self.url_card.hide()
        root.addWidget(self.url_card)

        # log
        self.log_btn = QPushButton("Show details  ▾")
        self.log_btn.setObjectName("link")
        self.log_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_btn.clicked.connect(self._toggle_log)
        root.addWidget(self.log_btn)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(140)
        self.log.hide()
        root.addWidget(self.log)

        root.addStretch()
        foot = label("by SUNSOFT software solutions", "muted")
        foot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(foot)

    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_icon(), self)
        menu = QMenu()
        a_show = QAction("Show Tunnelo", self)
        a_show.triggered.connect(self._show_window)
        self.a_copy = QAction("Copy public link", self)
        self.a_copy.triggered.connect(self._copy)
        self.a_copy.setEnabled(False)
        self.a_stop = QAction("Stop tunnel", self)
        self.a_stop.triggered.connect(self._stop)
        self.a_stop.setEnabled(False)
        a_quit = QAction("Quit", self)
        a_quit.triggered.connect(self._quit)
        for a in (a_show, self.a_copy, self.a_stop):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(a_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._show_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.setToolTip(APP_NAME)
        self.tray.show()
        self._tray_menu = menu

    # ---- actions ------------------------------------------------------- #
    def _show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _copy(self):
        if not self.url:
            return
        QGuiApplication.clipboard().setText(self.url)
        self.copy_btn.setText("Copied  ✓")
        QTimer.singleShot(1600, lambda: self.copy_btn.setText("Copy link"))

    def _toggle_log(self):
        show = not self.log.isVisible()
        self.log.setVisible(show)
        self.log_btn.setText("Hide details  ▴" if show else "Show details  ▾")
        self.resize(self.width(), self.height() + (170 if show else -170))

    def _on_toggle(self):
        if self.worker or self.dl:
            self._stop()
        else:
            self._start()

    def _set_running_ui(self, running: bool):
        self.input.setEnabled(not running)
        self.btn.setText("Stop Tunnel" if running else "Start Tunnel")
        self.btn.setProperty("running", "true" if running else "false")
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)
        if self.tray:
            self.a_stop.setEnabled(running)

    def _start(self):
        try:
            target, host, port = normalize_target(self.input.text())
        except ValueError as e:
            self.err.setText(str(e))
            self.err.show()
            self.dot.set_state("error")
            return
        self.err.hide()
        self.settings.setValue("address", self.input.text().strip())
        self._target = (target, host, port)
        self.log.clear()
        self._set_running_ui(True)
        self.local_chip.show()
        self.local_chip.setText("Checking your local app...")
        engine = find_engine()
        if engine:
            self._launch(engine)
        else:
            self._download()

    def _download(self):
        d = DownloadWorker(self)
        self.dl = d
        self.dot.set_state("connecting")
        self.status.setText("Downloading tunnel engine (one time)...")
        d.progress.connect(lambda n, d=d: self.dl is d and self.status.setText(f"Downloading tunnel engine...  {n}%"))
        d.done.connect(lambda path, d=d: self._on_downloaded(d, path))
        d.failed.connect(lambda msg, d=d: self._on_download_failed(d, msg))
        d.start()

    def _on_downloaded(self, d: DownloadWorker, path: str):
        if d is not self.dl:
            return
        self.dl = None
        self._launch(path)

    def _on_download_failed(self, d: DownloadWorker, msg: str):
        if d is not self.dl:
            return
        self.dl = None
        self._set_idle()
        self.err.setText(msg)
        self.err.show()
        self.dot.set_state("error")
        self.status.setText("Download failed")

    def _launch(self, engine: str):
        target, host, port = self._target
        w = TunnelWorker(engine, target, host, port, self)
        self.worker = w
        self.restart_count = 0
        self.started_at = time.time()
        keep_awake(True)
        w.state.connect(lambda k, m, w=w: self._on_state(w, k, m))
        w.url_ready.connect(lambda u, w=w: self._on_url(w, u))
        w.log_line.connect(lambda s, w=w: self.worker is w and self.log.appendPlainText(s))
        w.reconnects.connect(lambda n, w=w: self._on_restarts(w, n))
        w.local_state.connect(lambda ok, w=w: self._on_local(w, ok))
        w.start()

    def _stop(self):
        w, d = self.worker, self.dl
        self.worker = self.dl = None
        if d:
            d.cancel()
        if w:
            w.stop()
            w.wait(8000)
        self._set_idle()

    def _set_idle(self):
        self._set_running_ui(False)
        self.dot.set_state("idle")
        self.status.setText("Ready to start")
        self.url_card.hide()
        self.local_chip.hide()
        self.uptime.setText("")
        self.started_at = None
        self.url = ""
        keep_awake(False)
        if self.tray:
            self.a_copy.setEnabled(False)
            self.tray.setToolTip(APP_NAME)

    def _quit(self):
        self._quitting = True
        self.close()

    # ---- worker callbacks --------------------------------------------- #
    def _on_state(self, w, key: str, msg: str):
        if w is not self.worker:
            return
        self.dot.set_state(key)
        self.status.setText(msg)
        if key != "live":
            self.url_card.hide()
            if self.tray:
                self.a_copy.setEnabled(False)
        if self.tray:
            self.tray.setToolTip(f"{APP_NAME} - {msg}")

    def _on_url(self, w, url: str):
        if w is not self.worker:
            return
        is_new_after_restart = bool(self.url) and url != self.url
        self.url = url
        self.url_label.setText(url)
        self.url_card.show()
        self.copy_btn.setText("Copy link")
        if self.tray:
            self.a_copy.setEnabled(True)
            if is_new_after_restart or self.restart_count > 0:
                self.tray.showMessage(APP_NAME, f"Tunnel reconnected. New link:\n{url}",
                                      QSystemTrayIcon.MessageIcon.Information, 6000)

    def _on_restarts(self, w, n: int):
        if w is self.worker:
            self.restart_count = n

    def _on_local(self, w, ok: bool):
        if w is not self.worker:
            return
        host, port = self._target[1], self._target[2]
        if ok:
            self.local_chip.setText(f"<span style='color:#2ED573'>●</span>  Local app reachable at {host}:{port}")
        else:
            self.local_chip.setText(
                f"<span style='color:#FFA502'>●</span>  Nothing is listening on {host}:{port} yet - start your app")

    def _tick(self):
        if self.started_at is None:
            return
        s = int(time.time() - self.started_at)
        t = f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
        extra = f"  ·  {self.restart_count} restart{'s' if self.restart_count != 1 else ''}" if self.restart_count else ""
        self.uptime.setText(f"Session {t}{extra}")

    # ---- window events ------------------------------------------------- #
    def closeEvent(self, e):
        if not self._quitting and self.worker and self.tray and self.tray.isVisible():
            e.ignore()
            self.hide()
            if not self._tray_hint:
                self._tray_hint = True
                self.tray.showMessage(APP_NAME, "Still tunneling in the background. Use the tray icon to stop or quit.",
                                      QSystemTrayIcon.MessageIcon.Information, 4000)
            return
        self._stop()
        e.accept()
        QApplication.quit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont("Segoe UI", 10) if IS_WIN else app.font())
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
