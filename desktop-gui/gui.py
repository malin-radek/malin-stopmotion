"""
Stop-Motion Desktop GUI – PyQt6
Łączy się z Docker backendem przez http://localhost:8000

Instalacja:
    pip install PyQt6 opencv-python-headless requests

Uruchom:
    python gui.py
"""

import json
import sys
import time
from pathlib import Path
from threading import Thread
from typing import Optional

import cv2
import numpy as np
import requests
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread
from PyQt6.QtGui import (
    QColor, QFont, QImage, QKeySequence, QPixmap, QShortcut
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSlider, QSpinBox, QSplitter, QStatusBar,
    QVBoxLayout, QWidget
)

BACKEND = "http://localhost:8000"
DEFAULT_PROJECT = "default"
SETTINGS_FILE = Path.home() / ".stopmotion" / "settings.json"


# ── Camera thread: pobiera MJPEG ze /stream ──────────────────────────────────

class CameraWorker(QObject):
    frame_ready = pyqtSignal(np.ndarray)

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self._running = False

    def start(self):
        self._running = True
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        cap = cv2.VideoCapture(self.url)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        while self._running:
            ret, frame = cap.read()
            if ret:
                self.frame_ready.emit(frame)
            else:
                time.sleep(0.1)

        cap.release()


# ── Settings Helper ───────────────────────────────────────────────────────────

def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"project": DEFAULT_PROJECT}

def _save_settings(settings: dict):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f)

# ── Main Window ───────────────────────────────────────────────────────────────

class StopMotionGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stop-Motion Studio")
        self.setMinimumSize(1200, 700)

        # Załaduj zapisane ustawienia
        self.settings = _load_settings()
        
        self.project = self.settings.get("project", DEFAULT_PROJECT)
        self.frames: list[dict] = []
        self.onion_alpha = 0.35
        self.onion_enabled = True
        self.onion_layers = 2  # ile poprzednich klatek pokazywać

        self.current_live: Optional[np.ndarray] = None
        self.last_captured: list[np.ndarray] = []  # bufor onion skin

        self._build_ui()
        self._apply_style()
        self._setup_shortcuts()
        self._start_camera()
        self._refresh_frames()

    # ── UI Layout ──────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # ── Left panel: kamera + onion ──────────────────────────────────
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # Podgląd kamery
        self.preview_label = QLabel("Oczekiwanie na strumień...")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(640, 480)
        self.preview_label.setStyleSheet("background: #111; border: 1px solid #333; border-radius: 6px;")
        left_layout.addWidget(self.preview_label, stretch=1)

        # Toolbar kamery
        cam_bar = QHBoxLayout()

        self.capture_btn = QPushButton("⬤  CAPTURE  [SPACJA]")
        self.capture_btn.setFixedHeight(48)
        self.capture_btn.setStyleSheet(
            "background: #E8593C; color: white; font-size: 15px; font-weight: bold; border-radius: 6px;"
            "border: none;"
        )
        self.capture_btn.clicked.connect(self.capture_frame)
        cam_bar.addWidget(self.capture_btn, stretch=2)

        self.undo_btn = QPushButton("↩  Undo  [Z]")
        self.undo_btn.setFixedHeight(48)
        self.undo_btn.clicked.connect(self.undo_last)
        cam_bar.addWidget(self.undo_btn)

        left_layout.addLayout(cam_bar)

        # Onion skin controls
        onion_bar = QHBoxLayout()

        self.onion_toggle = QPushButton("Onion: ON")
        self.onion_toggle.setCheckable(True)
        self.onion_toggle.setChecked(True)
        self.onion_toggle.clicked.connect(self._toggle_onion)
        onion_bar.addWidget(self.onion_toggle)

        onion_bar.addWidget(QLabel("Przezroczystość:"))
        self.alpha_slider = QSlider(Qt.Orientation.Horizontal)
        self.alpha_slider.setRange(0, 100)
        self.alpha_slider.setValue(int(self.onion_alpha * 100))
        self.alpha_slider.valueChanged.connect(lambda v: setattr(self, "onion_alpha", v / 100))
        onion_bar.addWidget(self.alpha_slider)

        onion_bar.addWidget(QLabel("Warstwy:"))
        self.layers_spin = QSpinBox()
        self.layers_spin.setRange(1, 4)
        self.layers_spin.setValue(self.onion_layers)
        self.layers_spin.valueChanged.connect(lambda v: setattr(self, "onion_layers", v))
        onion_bar.addWidget(self.layers_spin)

        left_layout.addLayout(onion_bar)

        splitter.addWidget(left)

        # ── Right panel: projekt + klatki ──────────────────────────────
        right = QWidget()
        right.setMaximumWidth(340)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Projekt header
        proj_bar = QHBoxLayout()
        proj_bar.addWidget(QLabel("Projekt:"))
        self.project_input = QLineEdit(self.project)
        self.project_input.returnPressed.connect(self._change_project)
        proj_bar.addWidget(self.project_input)
        right_layout.addLayout(proj_bar)

        # FPS
        fps_bar = QHBoxLayout()
        fps_bar.addWidget(QLabel("FPS:"))
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 30)
        self.fps_spin.setValue(12)
        self.fps_spin.valueChanged.connect(self._set_fps)
        fps_bar.addWidget(self.fps_spin)

        self.play_btn = QPushButton("▶ Odtwórz")
        self.play_btn.clicked.connect(self._play_preview)
        fps_bar.addWidget(self.play_btn)
        right_layout.addLayout(fps_bar)

        # Lista klatek (miniatury)
        right_layout.addWidget(QLabel("Klatki:"))
        self.frame_list = QListWidget()
        self.frame_list.setIconSize(self.frame_list.iconSize().__class__(120, 90))
        self.frame_list.setSpacing(4)
        right_layout.addWidget(self.frame_list, stretch=1)

        # Backend IP
        ip_bar = QHBoxLayout()
        ip_bar.addWidget(QLabel("Backend:"))
        self.backend_input = QLineEdit(BACKEND)
        ip_bar.addWidget(self.backend_input)
        right_layout.addLayout(ip_bar)

        splitter.addWidget(right)
        splitter.setSizes([860, 340])

        # Statusbar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Gotowy")

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #1a1a1a;
                color: #e0e0e0;
                font-family: 'Segoe UI', 'SF Pro Display', sans-serif;
                font-size: 13px;
            }
            QPushButton {
                background: #2d2d2d;
                color: #e0e0e0;
                border: 1px solid #444;
                border-radius: 5px;
                padding: 6px 12px;
            }
            QPushButton:hover { background: #3a3a3a; }
            QPushButton:pressed { background: #222; }
            QPushButton:checked { background: #3B8BD4; border-color: #3B8BD4; }
            QLineEdit, QSpinBox, QComboBox {
                background: #2d2d2d;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 4px 8px;
                color: #e0e0e0;
            }
            QSlider::groove:horizontal {
                height: 4px;
                background: #444;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #3B8BD4;
                width: 14px;
                height: 14px;
                margin: -5px 0;
                border-radius: 7px;
            }
            QListWidget {
                background: #111;
                border: 1px solid #333;
                border-radius: 5px;
            }
            QListWidget::item:selected { background: #3B8BD4; }
            QStatusBar { background: #111; color: #888; }
            QSplitter::handle { background: #333; }
        """)

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Space"), self).activated.connect(self.capture_frame)
        QShortcut(QKeySequence("Z"), self).activated.connect(self.undo_last)
        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(self.undo_last)

    # ── Camera ─────────────────────────────────────────────────────────────

    def _start_camera(self):
        url = f"{BACKEND}/stream"
        self.camera = CameraWorker(url)
        self.camera.frame_ready.connect(self._on_frame)
        self.camera.start()

    def _on_frame(self, frame: np.ndarray):
        self.current_live = frame
        self._render_preview(frame)

    def _render_preview(self, frame: np.ndarray):
        """Nakłada onion skin na aktywny podgląd."""
        display = frame.copy()

        if self.onion_enabled and self.last_captured:
            n = min(self.onion_layers, len(self.last_captured))
            for i, prev_frame in enumerate(reversed(self.last_captured[-n:])):
                # Coraz bardziej przezroczyste starsze klatki
                alpha = self.onion_alpha * (1.0 - i * 0.25)
                if prev_frame.shape == display.shape:
                    # Koloruj onion: czerwony dla ostatniej, niebieska dla wcześniejszych
                    tinted = prev_frame.copy()
                    if i == 0:
                        tinted[:, :, 0] = 0   # usuń niebieski – zostaw czerwień
                    else:
                        tinted[:, :, 2] = 0   # usuń czerwony – zostaw niebiet

                    display = cv2.addWeighted(display, 1.0, tinted, alpha, 0)

        # Skaluj do rozmiaru widgetu
        h, w = display.shape[:2]
        label_w = self.preview_label.width()
        label_h = self.preview_label.height()
        scale = min(label_w / w, label_h / h)
        new_w, new_h = int(w * scale), int(h * scale)
        display = cv2.resize(display, (new_w, new_h))

        # BGR → RGB → QPixmap
        rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, new_w, new_h, new_w * 3, QImage.Format.Format_RGB888)
        self.preview_label.setPixmap(QPixmap.fromImage(qimg))

    # ── Capture / Undo ─────────────────────────────────────────────────────

    def capture_frame(self):
        project = self.project_input.text().strip() or DEFAULT_PROJECT
        try:
            r = requests.post(f"{BACKEND}/frame/take", params={"project": project}, timeout=5)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self.status_bar.showMessage(f"Błąd capture: {e}")
            return

        # Dodaj bieżącą klatkę do bufora onion skin
        if self.current_live is not None:
            self.last_captured.append(self.current_live.copy())
            if len(self.last_captured) > 5:
                self.last_captured.pop(0)

        self.status_bar.showMessage(f"Zapisano klatkę {data['frame_id']}  ({len(self.frames)+1} klatek)")
        self._refresh_frames()

    def undo_last(self):
        if not self.frames:
            return
        last = self.frames[-1]
        try:
            project = self.project_input.text().strip() or DEFAULT_PROJECT
            r = requests.delete(f"{BACKEND}/frames/{last['id']}", params={"project": project}, timeout=5)
            r.raise_for_status()
        except Exception as e:
            self.status_bar.showMessage(f"Błąd undo: {e}")
            return

        if self.last_captured:
            self.last_captured.pop()

        self.status_bar.showMessage(f"Cofnięto klatkę {last['id']}")
        self._refresh_frames()

    # ── Project / FPS ──────────────────────────────────────────────────────

    def _change_project(self):
        self.project = self.project_input.text().strip() or DEFAULT_PROJECT
        self.settings["project"] = self.project
        _save_settings(self.settings)
        self._refresh_frames()

    def _set_fps(self, fps: int):
        project = self.project_input.text().strip() or DEFAULT_PROJECT
        try:
            requests.post(f"{BACKEND}/projects/{project}/fps", params={"fps": fps}, timeout=3)
        except Exception:
            pass

    def _refresh_frames(self):
        project = self.project_input.text().strip() or DEFAULT_PROJECT
        try:
            r = requests.get(f"{BACKEND}/frames", params={"project": project}, timeout=5)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self.status_bar.showMessage(f"Brak połączenia z backendem: {e}")
            return

        self.frames = data.get("frames", [])
        fps = data.get("fps", 12)
        self.fps_spin.blockSignals(True)
        self.fps_spin.setValue(fps)
        self.fps_spin.blockSignals(False)

        self.frame_list.clear()
        for f in self.frames:
            item = QListWidgetItem(f"#{f['id']}")
            self.frame_list.addItem(item)

        self.status_bar.showMessage(f"Projekt '{project}': {len(self.frames)} klatek @ {fps} fps")

    # ── Onion toggle ───────────────────────────────────────────────────────

    def _toggle_onion(self):
        self.onion_enabled = self.onion_toggle.isChecked()
        self.onion_toggle.setText("Onion: ON" if self.onion_enabled else "Onion: OFF")

    # ── Preview playback ───────────────────────────────────────────────────

    def _play_preview(self):
        """Odtwarza zapisane klatki jako animację w oknie podglądu."""
        project = self.project_input.text().strip() or DEFAULT_PROJECT
        fps = self.fps_spin.value()

        if not self.frames:
            self.status_bar.showMessage("Brak klatek do odtworzenia")
            return

        self._playing = True
        self.play_btn.setText("■ Stop")
        self.play_btn.clicked.disconnect()
        self.play_btn.clicked.connect(self._stop_preview)

        self._play_thread = Thread(target=self._playback_loop, args=(project, fps), daemon=True)
        self._play_thread.start()

    def _stop_preview(self):
        self._playing = False
        self.play_btn.setText("▶ Odtwórz")
        self.play_btn.clicked.disconnect()
        self.play_btn.clicked.connect(self._play_preview)

    def _playback_loop(self, project: str, fps: int):
        delay = 1.0 / fps
        while self._playing:
            for frame_info in self.frames:
                if not self._playing:
                    break
                try:
                    r = requests.get(
                        f"{BACKEND}/frame/{frame_info['id']}.jpg",
                        params={"project": project},
                        timeout=3,
                    )
                    if r.ok:
                        arr = np.frombuffer(r.content, np.uint8)
                        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if img is not None:
                            self._render_preview(img)
                except Exception:
                    pass
                time.sleep(delay)

    # ── Cleanup ────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self.camera.stop()
        super().closeEvent(event)


# ── Entry ─────────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Stop-Motion Studio")
    window = StopMotionGUI()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
