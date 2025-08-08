import os
import sys
import subprocess
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QLineEdit,
    QComboBox,
    QRadioButton,
    QGroupBox,
    QListWidget,
    QProgressBar,
    QTextEdit,
    QMessageBox,
    QStatusBar,
    QFileDialog,
    QMenu,
    QCheckBox,
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QPoint
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QIntValidator

import ffmpeg
from ffmpeg_progress_yield import FfmpegProgress

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("conversion.log"), logging.StreamHandler()]
)

# ---- Global bounds for rate control ----
BITRATE_MIN_KBPS = 50        # practical floor; below this is usually unwatchable
BITRATE_MAX_KBPS = 200_000   # 200 Mbps ceiling; adjust if you really need more

CRF_MIN        = 0           # lower = better quality, larger files
CRF_MAX_HEVC   = 51          # x265 / NVENC HEVC typical max
CRF_MAX_AV1    = 63          # libaom / SVT-AV1 typical max (NVENC AV1 uses ~0-51 but 0-63 is safe UI)


# -----------------------------------------------------------------------------
# Helpers / Data
# -----------------------------------------------------------------------------
@dataclass
class ProbeInfo:
    duration: float
    vcodec: Optional[str]
    acodec: Optional[str]
    audio_bitrate_kbps: Optional[float]


def seconds_to_hhmmss(sec: int) -> str:
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_hhmmss(time_str: str) -> Optional[int]:
    """Parse strings like HH:MM:SS, MM:SS, or SS into seconds."""
    if not time_str:
        return None
    try:
        parts = time_str.strip().split(":")
        parts = [p.strip() for p in parts]
        if len(parts) == 3:
            h, m, s = map(int, parts)
        elif len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(parts[1])
        elif len(parts) == 1:
            h, m, s = 0, 0, int(parts[0])
        else:
            return None
        if m >= 60 or s >= 60 or min(h, m, s) < 0:
            return None
        return h * 3600 + m * 60 + s
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Conversion Thread
# -----------------------------------------------------------------------------
class ConversionThread(QThread):
    progress_updated = pyqtSignal(int)          # overall percent 0-100
    log_message = pyqtSignal(str, str)          # message, color
    per_file_label = pyqtSignal(str)            # "(2/5) filename.mp4 — 73%"
    finished = pyqtSignal()

    def __init__(
        self,
        files: List[str],
        codec: str,                              # "AV1" or "H.265"
        container: str,                          # "MP4" or "MKV"
        rate_mode: str,                          # "bitrate" or "quality"
        bitrate_kbps: Optional[int],             # when rate_mode == "bitrate"
        crf_cq_value: Optional[int],             # when rate_mode == "quality"
        preset: str,
        custom_output_dir: Optional[str],
        crop_settings: Dict[str, Tuple[int, int]],
        audio_copy: bool,
        audio_codec: str,
        audio_bitrate_kbps: int,
        smart_copy_when_same_codec: bool,
        parent=None,
    ):
        super().__init__(parent)
        self.files = files
        self.codec = codec
        self.container = container
        self.rate_mode = rate_mode
        self.bitrate_kbps = bitrate_kbps
        self.crf_cq_value = crf_cq_value
        self.preset = preset
        self.custom_output_dir = custom_output_dir
        self.crop_settings = crop_settings or {}
        self.audio_copy = audio_copy
        self.audio_codec = audio_codec
        self.audio_bitrate_kbps = audio_bitrate_kbps
        self.smart_copy_when_same_codec = smart_copy_when_same_codec

        self._is_running = True
        self.process = None
        self._probe_cache: Dict[str, ProbeInfo] = {}

    # -------------------------- ffmpeg helpers --------------------------
    @staticmethod
    def available_encoders() -> List[str]:
        try:
            out = subprocess.check_output(['ffmpeg', '-hide_banner', '-encoders'], stderr=subprocess.STDOUT)
            text = out.decode(errors='ignore')
            names = []
            for line in text.splitlines():
                if line.startswith(' '):
                    # lines look like: " V..... libx265 ..."
                    parts = line.split()
                    if len(parts) >= 2:
                        names.append(parts[1])
            return names
        except Exception:
            return []

    def choose_encoder(self, encoders: List[str]) -> str:
        if self.codec == "H.265":
            if 'hevc_nvenc' in encoders:
                return 'hevc_nvenc'
            return 'libx265'
        else:  # AV1
            if 'av1_nvenc' in encoders:
                return 'av1_nvenc'
            if 'libsvtav1' in encoders:
                return 'libsvtav1'
            return 'libaom-av1'  # slower, but widely available

    @staticmethod
    def map_preset(encoder: str, ui_preset: str) -> str:
        """Map UI preset labels to actual encoder presets.
        - NVENC (hevc_nvenc/av1_nvenc): p1..p7 (p1 fastest, p7 slowest)
        - libsvtav1: 0..13 (0 slowest/best, 13 fastest)
        - libx265/libaom-av1: accept ultrafast..veryslow directly
        """
        nvenc_map = {
            'ultrafast': 'p1',
            'superfast': 'p2',
            'veryfast': 'p3',
            'faster': 'p4',
            'fast': 'p5',
            'medium': 'p6',
            'slow': 'p7',
            'slower': 'p7',
            'veryslow': 'p7',
        }
        svt_map = {
            'ultrafast': '13',
            'superfast': '12',
            'veryfast': '10',
            'faster': '9',
            'fast': '8',
            'medium': '6',
            'slow': '4',
            'slower': '3',
            'veryslow': '2',
        }
        if encoder in ('hevc_nvenc', 'av1_nvenc'):
            return nvenc_map.get(ui_preset, 'p6')
        if encoder == 'libsvtav1':
            return svt_map.get(ui_preset, '6')
        return ui_preset  # libx265 / libaom-av1

    def probe(self, file: str) -> ProbeInfo:
        if file in self._probe_cache:
            return self._probe_cache[file]
        try:
            info = ffmpeg.probe(file)
            duration = float(info['format'].get('duration', 0.0))
            vcodec = None
            acodec = None
            abitrate = None
            for stream in info.get('streams', []):
                if stream.get('codec_type') == 'video' and not vcodec:
                    vcodec = stream.get('codec_name')
                if stream.get('codec_type') == 'audio' and not acodec:
                    acodec = stream.get('codec_name')
                    br = stream.get('bit_rate')
                    if br is not None:
                        try:
                            abitrate = float(br) / 1000.0
                        except Exception:
                            abitrate = None
            pi = ProbeInfo(duration=duration, vcodec=vcodec, acodec=acodec, audio_bitrate_kbps=abitrate)
            self._probe_cache[file] = pi
            return pi
        except Exception as e:
            self.log_message.emit(f"ffprobe failed for {file}: {e}", "red")
            pi = ProbeInfo(duration=0.0, vcodec=None, acodec=None, audio_bitrate_kbps=None)
            self._probe_cache[file] = pi
            return pi

    # --------------------------- Thread logic ---------------------------
    def run(self):
        encoders = self.available_encoders()
        encoder = self.choose_encoder(encoders)
        self.log_message.emit(f"Selected encoder: {encoder}", "black")
        mapped_preset = self.map_preset(encoder, self.preset)
        self.log_message.emit(f"Using preset '{mapped_preset}' (mapped from '{self.preset}')", "black")

        # Build totals for progress
        durations = []
        for f in self.files:
            p = self.probe(f)
            # Respect crop duration if set
            if f in self.crop_settings:
                s, e = self.crop_settings[f]
                dur = max(0, e - s)
            else:
                dur = int(p.duration or 0)
            durations.append(dur)
        total_duration = sum(durations) or 1

        done_duration = 0.0
        for idx, file in enumerate(self.files):
            if not self._is_running:
                break
            if not os.path.exists(file):
                self.log_message.emit(f"File not found: {file}. Skipping.", "red")
                continue

            try:
                pinfo = self.probe(file)
                in_vcodec = (pinfo.vcodec or '').lower()
                desired_vcodec = 'av1' if self.codec == 'AV1' else 'hevc'
                target_dur = durations[idx]

                # Output path
                input_dir = os.path.dirname(file)
                base = os.path.splitext(os.path.basename(file))[0]
                out_dir = self.custom_output_dir or input_dir
                os.makedirs(out_dir, exist_ok=True)
                out_path = os.path.join(out_dir, f"{base}_converted.{self.container.lower()}")
                if os.path.exists(out_path):
                    out_path = os.path.join(out_dir, f"{base}_converted_{int(time.time())}.{self.container.lower()}")

                # Decide copy vs encode
                do_stream_copy = False
                if self.smart_copy_when_same_codec and in_vcodec == desired_vcodec:
                    # Only safe to copy when user didn't request trimming, or trimming is OK with stream copy
                    do_stream_copy = True

                cmd = ['ffmpeg', '-hide_banner', '-nostats']

                # Seeking / trimming
                crop = self.crop_settings.get(file)
                if crop is not None:
                    start, end = crop
                    # Use -ss before -i for speed, -to for clarity
                    cmd.extend(['-ss', str(start)])

                cmd.extend(['-i', file])

                if crop is not None:
                    start, end = crop
                    cmd.extend(['-to', str(end)])  # since we used -ss before -i, -to is absolute timestamp

                # Include all streams by default
                cmd.extend(['-map', '0'])

                # Container-specific flags
                if self.container.upper() == 'MP4':
                    cmd.extend(['-movflags', '+faststart'])

                if do_stream_copy:
                    # Copy all streams as-is
                    cmd.extend(['-c', 'copy'])
                else:
                    # Video encode
                    cmd.extend(['-c:v', encoder, '-preset', mapped_preset])

                    # Rate control
                    if self.rate_mode == 'bitrate':
                        if self.bitrate_kbps is None or self.bitrate_kbps <= 0:
                            raise ValueError("Invalid bitrate")
                        if encoder in ('hevc_nvenc', 'av1_nvenc'):
                            # VBR with target bitrate and a reasonable buffer
                            b = str(self.bitrate_kbps) + 'k'
                            cmd.extend(['-rc:v', 'vbr', '-b:v', b, '-maxrate', b, '-bufsize', str(self.bitrate_kbps * 2) + 'k'])
                        else:
                            cmd.extend(['-b:v', str(self.bitrate_kbps) + 'k'])
                    else:  # quality mode
                        q = self.crf_cq_value if (self.crf_cq_value is not None and self.crf_cq_value >= 0) else 23
                        if encoder in ('hevc_nvenc', 'av1_nvenc'):
                            # NVENC constant quality
                            cmd.extend(['-rc:v', 'vbr', '-cq:v', str(q)])
                        elif encoder == 'libsvtav1':
                            cmd.extend(['-crf', str(q), '-b:v', '0'])
                        else:  # libx265 / libaom-av1
                            cmd.extend(['-crf', str(q)])

                    # Audio handling
                    if self.audio_copy:
                        cmd.extend(['-c:a', 'copy'])
                    else:
                        # Encode audio; default AAC for MP4, Opus for MKV if chosen
                        if self.audio_codec == 'AAC':
                            cmd.extend(['-c:a', 'aac', '-b:a', f"{self.audio_bitrate_kbps}k"])  # widely supported
                        elif self.audio_codec == 'Opus':
                            cmd.extend(['-c:a', 'libopus', '-b:a', f"{self.audio_bitrate_kbps}k"])  # great for MKV
                        else:
                            cmd.extend(['-c:a', 'copy'])

                    # Copy subtitles if present
                    cmd.extend(['-c:s', 'copy'])

                # Progress to stderr (pipe:2)
                cmd.extend(['-progress', 'pipe:2', out_path])

                self.log_message.emit(
                    f"Converting {os.path.basename(file)} → {os.path.basename(out_path)} using {self.codec}"
                    + (" (stream copy)" if do_stream_copy else f" ({encoder})"),
                    "black",
                )

                ff = FfmpegProgress(cmd)
                self.process = ff.process

                last_pct = -1
                for pct in ff.run_command_with_progress():
                    if not self._is_running:
                        self._terminate_process()
                        break
                    try:
                        pct_float = float(pct)
                    except Exception:
                        pct_float = 0.0

                    # Overall progress (weighted by duration)
                    overall = (done_duration + (pct_float / 100.0) * target_dur) / total_duration * 100.0
                    overall_int = max(0, min(100, int(overall)))

                    if overall_int != last_pct:
                        last_pct = overall_int
                        self.progress_updated.emit(overall_int)
                        self.per_file_label.emit(f"({idx + 1}/{len(self.files)}) {os.path.basename(file)} — {int(pct_float)}%")

                # If we finished this file without cancellation
                if self._is_running:
                    done_duration += target_dur
                    overall_int = max(0, min(100, int(done_duration / total_duration * 100.0)))
                    self.progress_updated.emit(overall_int)
                    self.log_message.emit(f"Completed: {out_path}", "green")

            except Exception as e:
                self.log_message.emit(f"Error converting {file}: {e}", "red")

        self.finished.emit()
        self._is_running = False

    def stop(self):
        self._is_running = False
        self._terminate_process()

    def _terminate_process(self):
        try:
            if self.process and self.process.poll() is None:
                # Try graceful stop first
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except Exception:
                    self.process.kill()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Drag & Drop list
# -----------------------------------------------------------------------------
class DragDropListWidget(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setToolTip("Drag and drop video files here")

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            urls = event.mimeData().urls()
            added_files = []
            for url in urls:
                file_path = url.toLocalFile()
                if self.is_video_file(file_path):
                    added_files.append(file_path)
            if added_files:
                self.window().add_dropped_files(added_files)

    @staticmethod
    def is_video_file(file_path: str) -> bool:
        valid_extensions = ('.mp4', '.mkv', '.avi', '.mov')
        return file_path.lower().endswith(valid_extensions)


# -----------------------------------------------------------------------------
# Main App
# -----------------------------------------------------------------------------
class VideoConverterApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python Video Batch Converter – Improved (bugfix)")
        self.setGeometry(100, 100, 760, 900)

        self.files: List[str] = []
        self.conversion_thread: Optional[ConversionThread] = None
        self.custom_output_dir: Optional[str] = None
        self.crop_settings: Dict[str, Tuple[int, int]] = {}

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

        # ---------------- File Selection ----------------
        file_layout = QHBoxLayout()
        select_btn = QPushButton("Select Files")
        select_btn.clicked.connect(self.select_files)
        clear_btn = QPushButton("Clear List")
        clear_btn.clicked.connect(self.clear_list)
        open_out_btn = QPushButton("Open Output Folder")
        open_out_btn.clicked.connect(self.open_output_folder)
        file_layout.addWidget(select_btn)
        file_layout.addWidget(clear_btn)
        file_layout.addWidget(open_out_btn)
        layout.addLayout(file_layout)

        self.file_list = DragDropListWidget(self)
        self.file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._file_list_menu)
        layout.addWidget(self.file_list)

        # ---------------- Crop Inputs ----------------
        self.crop_group = QGroupBox("Crop Video Duration")
        crop_layout = QHBoxLayout()
        crop_layout.addWidget(QLabel("Start:"))
        self.start_edit = QLineEdit("00:00:00")
        self.start_edit.setEnabled(False)
        crop_layout.addWidget(self.start_edit)
        crop_layout.addWidget(QLabel("End:"))
        self.end_edit = QLineEdit("00:00:00")
        self.end_edit.setEnabled(False)
        crop_layout.addWidget(self.end_edit)
        self.crop_group.setLayout(crop_layout)
        layout.addWidget(self.crop_group)
        self.file_list.itemSelectionChanged.connect(self.update_crop_inputs)
        self.start_edit.textChanged.connect(self.update_crop_settings)
        self.end_edit.textChanged.connect(self.update_crop_settings)

        # ---------------- Codec Selection ----------------
        codec_group = QGroupBox("Video Codec")
        codec_layout = QHBoxLayout()
        self.codec_av1 = QRadioButton("AV1")
        self.codec_h265 = QRadioButton("H.265")
        self.codec_h265.setChecked(True)
        self.codec_av1.toggled.connect(self._on_codec_change)
        codec_layout.addWidget(self.codec_av1)
        codec_layout.addWidget(self.codec_h265)
        codec_group.setLayout(codec_layout)
        layout.addWidget(codec_group)

        # ---------------- Container Selection ----------------
        container_group = QGroupBox("Container")
        container_layout = QHBoxLayout()
        self.container_mp4 = QRadioButton("MP4")
        self.container_mkv = QRadioButton("MKV")
        self.container_mp4.setChecked(True)
        container_layout.addWidget(self.container_mp4)
        container_layout.addWidget(self.container_mkv)
        container_group.setLayout(container_layout)
        layout.addWidget(container_group)

        # ---------------- Preset ----------------
        preset_label = QLabel("Preset (Speed vs Quality):")
        self.preset_combo = QComboBox()
        presets = ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow']
        self.preset_combo.addItems(presets)
        self.preset_combo.setCurrentText('medium')
        self.preset_combo.setToolTip("Faster = quicker conversion, lower quality; Slower = better quality")
        self.preset_combo.currentTextChanged.connect(self.update_estimate)
        layout.addWidget(preset_label)
        layout.addWidget(self.preset_combo)

        # ---------------- Rate Control ----------------
        rc_group = QGroupBox("Rate Control")
        rc_layout = QHBoxLayout()
        self.rate_mode_combo = QComboBox()
        self.rate_mode_combo.addItems(["Bitrate (kbps)", "Quality (CRF/CQ)"])
        self.rate_mode_combo.currentIndexChanged.connect(self._on_rate_mode_change)

        self.bitrate_label = QLabel("Bitrate:")
        self.bitrate_edit = QLineEdit("2000")
        # Use explicit min/max
        self.bitrate_edit.setValidator(QIntValidator(BITRATE_MIN_KBPS, BITRATE_MAX_KBPS))
        self.bitrate_edit.setToolTip(f"Target video bitrate in kbps ({BITRATE_MIN_KBPS}-{BITRATE_MAX_KBPS})")
        self.bitrate_edit.textChanged.connect(self.update_estimate)

        self.quality_label = QLabel("CRF/CQ:")
        self.quality_edit = QLineEdit("23")  # CRF/CQ value
        # Validator will be set dynamically based on codec (HEVC vs AV1)
        self.update_quality_validator()
        self.quality_edit.setToolTip("CRF (x265/libaom/libsvtav1) or CQ (NVENC); bounds depend on codec")
        self.quality_edit.textChanged.connect(self.update_estimate)
        self.quality_edit.setVisible(False)
        self.quality_label.setVisible(False)

        rc_layout.addWidget(self.rate_mode_combo)
        rc_layout.addWidget(self.bitrate_label)
        rc_layout.addWidget(self.bitrate_edit)
        rc_layout.addWidget(self.quality_label)
        rc_layout.addWidget(self.quality_edit)
        rc_group.setLayout(rc_layout)
        layout.addWidget(rc_group)


        # ---------------- Audio Options ----------------
        audio_group = QGroupBox("Audio")
        audio_layout = QHBoxLayout()
        self.audio_copy_chk = QCheckBox("Copy audio")
        self.audio_copy_chk.setChecked(True)
        self.audio_copy_chk.stateChanged.connect(self._on_audio_toggle)
        self.audio_codec_combo = QComboBox()
        self.audio_codec_combo.addItems(["AAC", "Opus"])  # simple set
        self.audio_codec_combo.setEnabled(False)
        self.audio_bitrate_edit = QLineEdit("160")
        self.audio_bitrate_edit.setValidator(QIntValidator(16, 1024))
        self.audio_bitrate_edit.setEnabled(False)
        audio_layout.addWidget(self.audio_copy_chk)
        audio_layout.addWidget(QLabel("Codec:"))
        audio_layout.addWidget(self.audio_codec_combo)
        audio_layout.addWidget(QLabel("kbps:"))
        audio_layout.addWidget(self.audio_bitrate_edit)
        audio_group.setLayout(audio_layout)
        layout.addWidget(audio_group)

        # ---------------- Smart Copy Toggle ----------------
        self.smart_copy_chk = QCheckBox("Auto stream copy when codec already matches")
        self.smart_copy_chk.setChecked(True)
        layout.addWidget(self.smart_copy_chk)

        # ---------------- Output Folder ----------------
        output_btn = QPushButton("Select Custom Output Folder")
        output_btn.clicked.connect(self.select_output_dir)
        layout.addWidget(output_btn)
        self.output_label = QLabel("Output: Same as input")
        layout.addWidget(self.output_label)

        # ---------------- Start/Cancel ----------------
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("Start Conversion")
        self.start_btn.clicked.connect(self.start_conversion)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_conversion)
        self.cancel_btn.setEnabled(False)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        # ---------------- Progress ----------------
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)
        self.current_file_label = QLabel("")
        layout.addWidget(self.current_file_label)

        # ---------------- Estimates ----------------
        self.estimate_label = QLabel("Estimated Total Size: N/A")
        layout.addWidget(self.estimate_label)
        self.estimate_time_label = QLabel("Estimated Time: N/A")
        layout.addWidget(self.estimate_time_label)

        # ---------------- Log Area ----------------
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)

    # -------------------------- UI helpers --------------------------
    def _file_list_menu(self, pos: QPoint):
        menu = QMenu(self)
        remove_selected = menu.addAction("Remove Selected")
        clear_all = menu.addAction("Clear All")
        action = menu.exec(self.file_list.mapToGlobal(pos))
        if action == remove_selected:
            for item in self.file_list.selectedItems():
                file = item.text()
                if file in self.files:
                    self.files.remove(file)
                self.crop_settings.pop(file, None)
            self.update_file_list()
            self.update_estimate()
        elif action == clear_all:
            self.clear_list()

    def _on_audio_toggle(self):
        en = not self.audio_copy_chk.isChecked()
        self.audio_codec_combo.setEnabled(en)
        self.audio_bitrate_edit.setEnabled(en)

    def _on_rate_mode_change(self):
        quality_mode = (self.rate_mode_combo.currentIndex() == 1)
        # Toggle bitrate widgets
        self.bitrate_label.setVisible(not quality_mode)
        self.bitrate_edit.setVisible(not quality_mode)
        # Toggle quality widgets
        self.quality_edit.setVisible(quality_mode)
        self.quality_label.setVisible(quality_mode)
        self.update_estimate()

    def _on_codec_change(self):
        # Adjust default CRF/CQ when switching codecs (rough guidance)
        if self.codec_av1.isChecked():
            self.quality_edit.setText("30")  # AV1 typical
        else:
            self.quality_edit.setText("23")  # HEVC typical

        # Refresh validator + label to reflect new codec's bounds
        self.update_quality_validator()
        self.update_estimate()


    def seconds_to_time(self, sec: int) -> str:
        return seconds_to_hhmmss(sec)

    def parse_time(self, s: str) -> Optional[int]:
        return parse_hhmmss(s)

    # -------------------------- Crop handling --------------------------
    def update_crop_inputs(self):
        selected = self.file_list.selectedItems()
        if len(selected) != 1:
            self.start_edit.setEnabled(False)
            self.end_edit.setEnabled(False)
            self.start_edit.setText("00:00:00")
            self.end_edit.setText("00:00:00")
            return
        file = selected[0].text()
        if file not in self.files:
            return
        try:
            info = ffmpeg.probe(file)
            duration = float(info['format']['duration'])
            if file in self.crop_settings:
                start, end = self.crop_settings[file]
                start_str = seconds_to_hhmmss(start)
                end_str = seconds_to_hhmmss(end)
            else:
                start_str = "00:00:00"
                end_str = seconds_to_hhmmss(int(duration))
            self.start_edit.setText(start_str)
            self.end_edit.setText(end_str)
            self.start_edit.setEnabled(True)
            self.end_edit.setEnabled(True)
        except Exception as e:
            self.log_message(f"Error getting duration for {file}: {e}", "red")
            self.start_edit.setEnabled(False)
            self.end_edit.setEnabled(False)

    def update_crop_settings(self):
        if not self.start_edit.isEnabled():
            return
        selected = self.file_list.selectedItems()
        if len(selected) != 1:
            return
        file = selected[0].text()
        start_str = self.start_edit.text()
        end_str = self.end_edit.text()
        start = self.parse_time(start_str)
        end = self.parse_time(end_str)
        if start is None or end is None or start >= end:
            return
        try:
            info = ffmpeg.probe(file)
            duration = float(info['format']['duration'])
            if end > duration:
                end = int(duration)
                self.end_edit.setText(seconds_to_hhmmss(end))
            if start > duration:
                start = 0
                self.start_edit.setText("00:00:00")
            if start >= end:
                return
            self.crop_settings[file] = (start, end)
            self.update_estimate()
        except Exception as e:
            self.log_message(f"Error getting duration for {file}: {e}", "red")

    # -------------------------- File mgmt --------------------------
    def add_dropped_files(self, files: List[str]):
        new_files = [f for f in files if f not in self.files]
        self.files.extend(new_files)
        self.update_file_list()
        self.update_estimate()
        if new_files:
            self.log_message(f"Added {len(new_files)} dropped file(s).", "blue")

    def select_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Videos", "", "Video Files (*.mp4 *.mkv *.avi *.mov)")
        new_files = [f for f in files if f not in self.files]
        self.files.extend(new_files)
        self.update_file_list()
        self.update_estimate()

    def clear_list(self):
        self.files.clear()
        self.crop_settings.clear()
        self.update_file_list()
        self.update_estimate()

    def update_file_list(self):
        self.file_list.clear()
        for f in self.files:
            self.file_list.addItem(f)

    # -------------------------- Output dir --------------------------
    def select_output_dir(self):
        self.custom_output_dir = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if self.custom_output_dir:
            self.output_label.setText(f"Output: {self.custom_output_dir}")
        else:
            self.output_label.setText("Output: Same as input")

    def open_output_folder(self):
        path = self.custom_output_dir or (os.path.dirname(self.files[0]) if self.files else None)
        if not path:
            QMessageBox.information(self, "Open Folder", "No output folder yet.")
            return
        try:
            if sys.platform.startswith('win'):
                os.startfile(path)
            elif sys.platform == 'darwin':
                subprocess.check_call(['open', path])
            else:
                subprocess.check_call(['xdg-open', path])
        except Exception as e:
            QMessageBox.warning(self, "Open Folder", f"Failed to open folder: {e}")

    # -------------------------- Estimates & Bounds --------------------------
    def get_codec(self) -> str:
        return "AV1" if self.codec_av1.isChecked() else "H.265"

    def get_container(self) -> str:
        return "MP4" if self.container_mp4.isChecked() else "MKV"

    # ---- bounds helpers ----
    def bitrate_bounds(self) -> tuple[int, int]:
        return (BITRATE_MIN_KBPS, BITRATE_MAX_KBPS)

    def quality_bounds(self) -> tuple[int, int]:
        # HEVC uses 0-51, AV1 typically 0-63
        return (CRF_MIN, CRF_MAX_AV1 if self.codec_av1.isChecked() else CRF_MAX_HEVC)

    def update_quality_validator(self) -> None:
        lo, hi = self.quality_bounds()
        self.quality_edit.setValidator(QIntValidator(lo, hi))
        # make the label reflect current bounds for clarity
        self.quality_label.setText(f"CRF/CQ ({lo}-{hi}):")

    @staticmethod
    def _clamp(v: int, lo: int, hi: int) -> int:
        return max(lo, min(hi, v))

    # ---- getters that clamp to bounds ----
    def get_bitrate(self) -> Optional[int]:
        txt = self.bitrate_edit.text().strip()
        if not txt:
            return None
        try:
            v = int(txt)
        except Exception:
            QMessageBox.warning(self, "Invalid Input", "Bitrate must be an integer (kbps).")
            return None
        lo, hi = self.bitrate_bounds()
        v_clamped = self._clamp(v, lo, hi)
        if v != v_clamped:
            # reflect the clamp in the UI so the user sees the enforced range
            self.bitrate_edit.setText(str(v_clamped))
        return v_clamped

    def get_quality_value(self) -> Optional[int]:
        txt = self.quality_edit.text().strip()
        if not txt:
            return None
        try:
            v = int(txt)
        except Exception:
            QMessageBox.warning(self, "Invalid Input", "CRF/CQ must be an integer.")
            return None
        lo, hi = self.quality_bounds()
        v_clamped = self._clamp(v, lo, hi)
        if v != v_clamped:
            self.quality_edit.setText(str(v_clamped))
        return v_clamped

    def update_estimate(self):
        if not self.files:
            self.estimate_label.setText("Estimated Total Size: N/A")
            self.estimate_time_label.setText("Estimated Time: N/A")
            return

        total_size_mb = 0.0
        total_time_sec = 0.0
        bitrate = self.get_bitrate()
        preset = self.preset_combo.currentText()
        codec = self.get_codec()
        has_nvenc = 'nvenc' in ''.join(ConversionThread.available_encoders()).lower()

        # Time estimate factors
        preset_factor = self.get_preset_time_factor(preset)
        codec_factor = 1.5 if codec == "AV1" else 1.0
        hardware_factor = 0.33 if has_nvenc else 1.0

        for f in self.files:
            try:
                info = ffmpeg.probe(f)
                full_duration = float(info['format']['duration'])
                if f in self.crop_settings:
                    s, e = self.crop_settings[f]
                    duration = max(0, e - s)
                else:
                    duration = full_duration

                # Size estimate (bitrate mode only)
                audio_bitrate_kbps = None
                for s in info.get('streams', []):
                    if s.get('codec_type') == 'audio':
                        br = s.get('bit_rate')
                        if br is not None:
                            try:
                                audio_bitrate_kbps = float(br) / 1000.0
                            except Exception:
                                audio_bitrate_kbps = None
                        break
                if audio_bitrate_kbps is None:
                    audio_bitrate_kbps = 128.0

                if self.rate_mode_combo.currentIndex() == 0:  # bitrate mode
                    if bitrate:
                        video_size_kb = bitrate * duration
                        audio_size_kb = (audio_bitrate_kbps * duration)
                        total_size_mb += ((video_size_kb + audio_size_kb) / 8192.0) * 1.1  # 10% mux overhead
                else:
                    total_size_mb = float('nan')  # unknown under CRF/CQ

                # Time estimate (very rough)
                file_time = duration * preset_factor * codec_factor * hardware_factor
                total_time_sec += file_time

            except Exception as e:
                self.log_message(f"Error probing {f}: {e}", "red")

        if self.rate_mode_combo.currentIndex() == 0 and not (bitrate is None):
            self.estimate_label.setText(f"Estimated Total Size: {total_size_mb:.2f} MB")
        else:
            self.estimate_label.setText("Estimated Total Size: depends on CRF/CQ")

        self.estimate_time_label.setText(f"Estimated Time: {total_time_sec / 60.0:.1f} min")

    @staticmethod
    def get_preset_time_factor(preset: str) -> float:
        factors = {
            'ultrafast': 0.5,
            'superfast': 0.7,
            'veryfast': 0.8,
            'faster': 1.0,
            'fast': 1.2,
            'medium': 1.5,
            'slow': 2.5,
            'slower': 4.0,
            'veryslow': 8.0,
        }
        return factors.get(preset, 1.5)


    # -------------------------- Convert / Cancel --------------------------
    def start_conversion(self):
        if self.conversion_thread and self.conversion_thread.isRunning():
            return

        if not self.files:
            QMessageBox.warning(self, "Error", "Select files first.")
            return

        rate_mode = 'bitrate' if self.rate_mode_combo.currentIndex() == 0 else 'quality'
        bitrate = self.get_bitrate() if rate_mode == 'bitrate' else None
        crf_cq = self.get_quality_value() if rate_mode == 'quality' else None

        if (rate_mode == 'bitrate' and bitrate is None) or (rate_mode == 'quality' and crf_cq is None):
            return

        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.current_file_label.setText("")
        self.status_bar.showMessage("Converting…")

        self.conversion_thread = ConversionThread(
            files=self.files.copy(),
            codec=self.get_codec(),
            container=self.get_container(),
            rate_mode=rate_mode,
            bitrate_kbps=bitrate,
            crf_cq_value=crf_cq,
            preset=self.preset_combo.currentText(),
            custom_output_dir=self.custom_output_dir,
            crop_settings=self.crop_settings.copy(),
            audio_copy=self.audio_copy_chk.isChecked(),
            audio_codec=self.audio_codec_combo.currentText(),
            audio_bitrate_kbps=int(self.audio_bitrate_edit.text()),
            smart_copy_when_same_codec=self.smart_copy_chk.isChecked(),
        )
        self.conversion_thread.progress_updated.connect(self.update_progress)
        self.conversion_thread.log_message.connect(self.log_message)
        self.conversion_thread.per_file_label.connect(self.current_file_label.setText)
        self.conversion_thread.finished.connect(self.conversion_finished)
        self.conversion_thread.start()

    def cancel_conversion(self):
        if self.conversion_thread and self.conversion_thread.isRunning():
            self.conversion_thread.stop()
            self.conversion_thread.wait()
            self.log_message("Conversion cancelled by user.", "orange")
            self.conversion_finished()

    def update_progress(self, value: int):
        self.progress_bar.setValue(int(value))

    def log_message(self, message: str, color: str = "black"):
        logging.info(message)
        self.log_text.append(f'<span style="color:{color};">{message}</span>')
        self.log_text.ensureCursorVisible()

    def conversion_finished(self):
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.status_bar.showMessage("Ready")
        self.conversion_thread = None
        QMessageBox.information(self, "Done", "Conversion completed or cancelled!")


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoConverterApp()
    window.show()
    sys.exit(app.exec())
