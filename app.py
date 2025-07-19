
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QFileDialog,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QListWidget,
    QProgressBar,
    QPlainTextEdit,
    QLabel,
    QSpinBox,
    QMessageBox,
    QComboBox,
)
from PySide6.QtCore import QProcess, Qt
import json

FRAME_PREFIX = "frame="


def check_dependencies():
    """Validate presence of ffmpeg and av1_nvenc encoder."""
    proc = QProcess()
    proc.start("ffmpeg", ["-version"])
    proc.waitForFinished()
    if proc.exitCode() != 0:
        QMessageBox.critical(None, "Dependency Error", "FFmpeg not found.")
        return False

    proc.start("ffmpeg", ["-hide_banner", "-encoders"])
    proc.waitForFinished()
    out = proc.readAllStandardOutput().data().decode()
    if "av1_nvenc" not in out:
        QMessageBox.critical(None, "Dependency Error", "FFmpeg build lacks av1_nvenc.")
        return False
    if "hevc_nvenc" not in out:
        QMessageBox.critical(None, "Dependency Error", "FFmpeg build lacks hevc_nvenc.")
        return False

    return True


class DropListWidget(QListWidget):
    """List widget that accepts files via drag and drop."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.is_file():
                self.addItem(str(path))
        event.acceptProposedAction()


class FFmpegTranscoder(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self.duration_ms = 1
        self.width = 0
        self.height = 0
        self.total_frames = 1

    def build_command(self, infile: Path, outdir: Path, bitrate_kbps: int, codec: str, fps: int):
        if codec == "AV1":
            c_v = "av1_nvenc"
            profile = "0"
            outfile = outdir / f"{infile.stem}_av1.mp4"
        else:  # HEVC
            c_v = "hevc_nvenc"
            profile = "main10"
            outfile = outdir / f"{infile.stem}_hevc.mp4"

        return [
            "ffmpeg",
            "-i",
            str(infile),
            "-r",
            str(fps),
            "-c:v",
            c_v,
            "-gpu",
            "0",
            "-profile:v",
            profile,
            "-pix_fmt",
            "p010le",
            "-rc",
            "vbr",
            "-b:v",
            f"{bitrate_kbps}k",
            "-preset",
            "p5",
            "-tune",
            "hq",
            "-g",
            "240",
            "-spatial_aq",
            "1",
            "-aq-strength",
            "8",
            "-movflags",
            "+faststart",
            "-c:a",
            "copy",
            "-map",
            "0",
            "-progress",
            "pipe:1",
            "-nostats",
            "-loglevel",
            "error",
            str(outfile),
        ]

    def probe_info(self, infile: Path):
        """Return (width, height, duration_ms) of the input video."""
        proc = QProcess()
        proc.start(
            "ffprobe",
            [
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(infile),
            ],
        )
        proc.waitForFinished()
        out = proc.readAllStandardOutput().data().decode()
        try:
            data = json.loads(out)
            stream = data["streams"][0]
            self.width = int(stream.get("width", 0))
            self.height = int(stream.get("height", 0))
            duration = float(data["format"].get("duration", 0))
            self.duration_ms = int(duration * 1000) or 1
        except Exception:
            self.width = self.height = 0
            self.duration_ms = 1
        return self.width, self.height, self.duration_ms

    def start(self, infile: Path, outdir: Path, bitrate_kbps: int):
        self.process = QProcess()
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        cmd = self.build_command(infile, outdir, bitrate_kbps)
        self.process.start(cmd[0], cmd[1:])

    def kill(self):
        if self.process:
            self.process.kill()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AV1 Batch Converter")

        self.file_list = DropListWidget()

        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(100, 100000)
        self.bitrate_spin.setValue(5000)
        self.bitrate_spin.valueChanged.connect(self.update_estimate)

        self.codec_combo = QComboBox()
        self.codec_combo.addItems(["AV1", "HEVC"])

        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["30", "60"])

        self.info_label = QLabel("Resolution: -\nDuration: -")
        self.size_label = QLabel("Estimated Size: -")
        self.file_list.currentRowChanged.connect(self.file_selected)

        self.browse_btn = QPushButton("Browse…")
        self.output_label = QLabel()
        self.browse_btn.clicked.connect(self.choose_outdir)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.batch_progress = QProgressBar()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)

        self.start_btn = QPushButton("Start")
        self.cancel_btn = QPushButton("Cancel")
        self.clear_btn = QPushButton("Clear")
        self.start_btn.clicked.connect(self.start_batch)
        self.cancel_btn.clicked.connect(self.cancel)
        self.clear_btn.clicked.connect(self.clear_all)
        self.cancel_btn.setEnabled(False)

        layout = QVBoxLayout()
        layout.addWidget(self.file_list)

        row = QHBoxLayout()
        row.addWidget(QLabel("Bitrate (kbps)"))
        row.addWidget(self.bitrate_spin)
        row.addStretch()
        layout.addLayout(row)

        options_row = QHBoxLayout()
        options_row.addWidget(QLabel("Codec"))
        options_row.addWidget(self.codec_combo)
        options_row.addStretch()
        options_row.addWidget(QLabel("Output FPS"))
        options_row.addWidget(self.fps_combo)
        layout.addLayout(options_row)

        info_row = QHBoxLayout()
        info_row.addWidget(self.info_label)
        info_row.addWidget(self.size_label)
        layout.addLayout(info_row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Output:"))
        row2.addWidget(self.output_label)
        row2.addWidget(self.browse_btn)
        layout.addLayout(row2)

        layout.addWidget(self.progress)
        layout.addWidget(self.batch_progress)
        layout.addWidget(self.log)

        row3 = QHBoxLayout()
        row3.addWidget(self.cancel_btn)
        row3.addWidget(self.clear_btn)
        row3.addStretch()
        row3.addWidget(self.start_btn)
        layout.addLayout(row3)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.transcoder = FFmpegTranscoder()
        self.file_info_cache = {}
        self.current_index = -1
        self.manual_outdir = False
        self.outdir = Path.cwd()
        self.output_label.setText("Same as input")
        self.log_file = None
        self.buffer = ""

    def choose_outdir(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select output directory", str(self.outdir)
        )
        if path:
            self.outdir = Path(path)
            self.manual_outdir = True
            self.output_label.setText(path)

    def start_batch(self):
        if self.file_list.count() == 0:
            return
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.current_index = 0
        if not self.manual_outdir:
            self.output_label.setText("Same as input")
        self.progress.setValue(0)
        self.batch_progress.setRange(0, self.file_list.count())
        self.batch_progress.setValue(0)
        self.process_next()

    def process_next(self):
        if self.current_index >= self.file_list.count():
            self.start_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            self.progress.setValue(0)
            self.batch_progress.setValue(self.file_list.count())
            return
        item = self.file_list.item(self.current_index)
        infile = Path(item.text())
        outdir = self.outdir if self.manual_outdir else infile.parent
        info = self.file_info_cache.get(infile)
        if not info:
            info = self.transcoder.probe_info(infile)
            self.file_info_cache[infile] = info

        self.transcoder.duration_ms = info[2]
        fps = int(self.fps_combo.currentText())
        duration_sec = info[2] / 1000.0
        self.transcoder.total_frames = max(1, int(duration_sec * fps + 0.5))
        codec = self.codec_combo.currentText()

        self.transcoder.process = QProcess()
        self.transcoder.process.setProcessChannelMode(QProcess.MergedChannels)
        self.transcoder.process.readyReadStandardOutput.connect(self.update_progress)
        self.transcoder.process.finished.connect(self.handle_finished)
        cmd = self.transcoder.build_command(
            infile,
            outdir,
            self.bitrate_spin.value(),
            codec,
            fps,
        )
        log_path = outdir / f"{infile.stem}.log"
        self.log_file = open(log_path, "w", encoding="utf-8")
        self.transcoder.process.start(cmd[0], cmd[1:])
        self.append_log(f"Started: {infile.name}")

    def update_progress(self):
        data = bytes(self.transcoder.process.readAllStandardOutput()).decode()
        self.buffer += data
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            line = line.strip()
            self.append_log(line)
            if line.startswith(FRAME_PREFIX):
                try:
                    frames = int(line[len(FRAME_PREFIX) :])
                    if self.transcoder.total_frames > 0:
                        pct = int((frames / self.transcoder.total_frames) * 100)
                        self.progress.setValue(min(pct, 100))
                except ValueError:
                    pass

    def append_log(self, text: str):
        """Write a line to the log widget and current log file."""
        self.log.appendPlainText(text.rstrip())
        if self.log_file:
            self.log_file.write(text + "\n")
            self.log_file.flush()

    def handle_finished(self):
        self.progress.setValue(100)
        self.append_log("Finished")
        if self.log_file:
            self.log_file.close()
            self.log_file = None
        self.current_index += 1
        self.batch_progress.setValue(self.current_index)
        self.progress.setValue(0)
        self.buffer = ""
        self.process_next()

    def cancel(self):
        if self.transcoder.process:
            self.transcoder.kill()
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.append_log("Cancelled")
        if self.log_file:
            self.log_file.close()
            self.log_file = None
        self.progress.setValue(0)
        self.batch_progress.setValue(0)
        self.buffer = ""

    def clear_all(self):
        """Reset the UI to its initial state."""
        self.cancel()
        self.file_list.clear()
        self.file_info_cache.clear()
        self.log.clear()
        self.manual_outdir = False
        self.output_label.setText("Same as input")
        self.buffer = ""
        self.current_index = -1

    def file_selected(self):
        item = self.file_list.currentItem()
        if not item:
            self.info_label.setText("Resolution: -\nDuration: -")
            self.size_label.setText("Estimated Size: -")
            return
        path = Path(item.text())
        info = self.file_info_cache.get(path)
        if not info:
            info = self.transcoder.probe_info(path)
            self.file_info_cache[path] = info
        width, height, duration_ms = info
        self.update_info_display(width, height, duration_ms)
        self.update_estimate()

    def update_info_display(self, width: int, height: int, duration_ms: int):
        hrs = duration_ms // 3600000
        mins = (duration_ms % 3600000) // 60000
        secs = (duration_ms % 60000) // 1000
        self.info_label.setText(
            f"Resolution: {width}x{height}\nDuration: {hrs:02}:{mins:02}:{secs:02}"
        )

    def update_estimate(self):
        item = self.file_list.currentItem()
        if not item:
            self.size_label.setText("Estimated Size: -")
            return
        path = Path(item.text())
        info = self.file_info_cache.get(path)
        if not info:
            return
        duration_ms = info[2]
        bitrate_kbps = self.bitrate_spin.value()
        size_mb = (bitrate_kbps / 8) * (duration_ms / 1000) / 1024
        self.size_label.setText(f"Estimated Size: {size_mb:.2f} MB")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    if check_dependencies():
        window = MainWindow()
        window.resize(800, 600)
        window.show()
        sys.exit(app.exec())
