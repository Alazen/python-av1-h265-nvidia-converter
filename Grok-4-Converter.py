import os
import sys
import subprocess
import logging
import time
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QLineEdit, QComboBox, QRadioButton, QGroupBox, QListWidget, 
                             QProgressBar, QTextEdit, QMessageBox, QStatusBar, QFileDialog)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
import ffmpeg
from ffmpeg_progress_yield import FfmpegProgress

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler("conversion.log"), logging.StreamHandler()])

class ConversionThread(QThread):
    progress_updated = pyqtSignal(int)  # Explicitly int for signal
    log_message = pyqtSignal(str, str)  # message, color
    finished = pyqtSignal()

    def __init__(self, files, codec, container, bitrate, preset, custom_output_dir, parent=None):
        super().__init__(parent)
        self.files = files
        self.codec = codec
        self.container = container
        self.bitrate = bitrate
        self.preset = preset
        self.custom_output_dir = custom_output_dir
        self._is_running = True
        self.process = None

    def run(self):
        has_nvenc = self.check_nvenc()
        encoder = None
        for file in self.files:
            if not self._is_running:
                break
            if not os.path.exists(file):
                self.log_message.emit(f"File not found: {file}. Skipping.", "red")
                continue
            try:
                input_dir = os.path.dirname(file)
                base_name = os.path.splitext(os.path.basename(file))[0]
                output_dir = self.custom_output_dir or input_dir
                output_file = os.path.join(output_dir, f"{base_name}_converted.{self.container.lower()}")
                
                # Avoid overwriting
                if os.path.exists(output_file):
                    output_file = os.path.join(output_dir, f"{base_name}_converted_{int(time.time())}.{self.container.lower()}")

                # Select encoder and map preset for NVENC
                if self.codec == "H.265":
                    encoder = 'hevc_nvenc' if has_nvenc else 'libx265'
                elif self.codec == "AV1":
                    encoder = 'av1_nvenc' if has_nvenc else 'libsvtav1'
                
                mapped_preset = self.map_preset_for_encoder(self.preset, encoder, has_nvenc)
                self.log_message.emit(f"Using preset '{mapped_preset}' for encoder '{encoder}'", "black")

                # Build FFmpeg command with progress output
                cmd = ['ffmpeg', '-i', file, '-preset', mapped_preset, '-b:v', self.bitrate + 'k', '-c:a', 'copy',
                       '-progress', 'pipe:2']  # Force progress to stderr for parsing
                cmd.extend(['-c:v', encoder, output_file])

                self.log_message.emit(f"Converting {file} to {output_file} with {self.codec} ({encoder})...", "black")

                ff = FfmpegProgress(cmd)
                self.process = ff.process  # For cancellation
                for progress in ff.run_command_with_progress():
                    if not self._is_running:
                        self.terminate_process()
                        break
                    int_progress = int(progress)  # Cast to int
                    self.progress_updated.emit(int_progress)
                    self.log_message.emit(f"Progress update: {progress:.2f}%", "blue")  # Debug: Remove after testing
                if self._is_running:
                    self.progress_updated.emit(100)  # Ensure it hits 100% on completion
                    self.log_message.emit(f"Completed: {output_file}", "green")
            except Exception as e:
                self.log_message.emit(f"Error converting {file}: {str(e)}", "red")
        self.finished.emit()
        self._is_running = False

    def stop(self):
        self._is_running = False
        self.terminate_process()

    def terminate_process(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait()

    def check_nvenc(self):
        try:
            output = subprocess.check_output(['ffmpeg', '-encoders']).decode()
            return 'nvenc' in output.lower()
        except:
            return False

    def map_preset_for_encoder(self, preset, encoder, has_nvenc):
        if not has_nvenc:
            return preset  # Software encoders use standard presets
        # Map to NVENC-compatible presets (e.g., for hevc_nvenc or av1_nvenc)
        nvenc_map = {
            'ultrafast': 'fast',
            'superfast': 'fast',
            'veryfast': 'fast',
            'faster': 'medium',
            'fast': 'medium',
            'medium': 'medium',
            'slow': 'slow',
            'slower': 'slow',
            'veryslow': 'slow'
        }
        return nvenc_map.get(preset, 'medium')  # Default to 'medium'

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
                self.parent().parent().add_dropped_files(added_files)  # Call parent's method to add files

    def is_video_file(self, file_path):
        valid_extensions = ('.mp4', '.mkv', '.avi', '.mov')
        return file_path.lower().endswith(valid_extensions)

class VideoConverterApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python Video Batch Converter")
        self.setGeometry(100, 100, 600, 800)
        self.files = []
        self.conversion_thread = None
        self.custom_output_dir = None

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

        # File Selection
        file_layout = QHBoxLayout()
        select_btn = QPushButton("Select Files")
        select_btn.clicked.connect(self.select_files)
        select_btn.setToolTip("Select multiple video files for conversion")
        clear_btn = QPushButton("Clear List")
        clear_btn.clicked.connect(self.clear_list)
        file_layout.addWidget(select_btn)
        file_layout.addWidget(clear_btn)
        layout.addLayout(file_layout)

        self.file_list = DragDropListWidget(self)  # Use custom drag-drop enabled list
        self.file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self.remove_file)
        layout.addWidget(self.file_list)

        # Codec Selection
        codec_group = QGroupBox("Video Codec")
        codec_layout = QHBoxLayout()
        self.codec_av1 = QRadioButton("AV1")
        self.codec_h265 = QRadioButton("H.265")
        self.codec_h265.setChecked(True)
        codec_layout.addWidget(self.codec_av1)
        codec_layout.addWidget(self.codec_h265)
        codec_group.setLayout(codec_layout)
        layout.addWidget(codec_group)

        # Container Selection
        container_group = QGroupBox("Container")
        container_layout = QHBoxLayout()
        self.container_mp4 = QRadioButton("MP4")
        self.container_mkv = QRadioButton("MKV")
        self.container_mp4.setChecked(True)
        container_layout.addWidget(self.container_mp4)
        container_layout.addWidget(self.container_mkv)
        container_group.setLayout(container_layout)
        layout.addWidget(container_group)

        # Preset Selection
        preset_label = QLabel("Preset (Speed vs Quality):")
        self.preset_combo = QComboBox()
        presets = ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow']
        self.preset_combo.addItems(presets)
        self.preset_combo.setCurrentText('medium')
        self.preset_combo.setToolTip("Faster presets = quicker conversion, lower quality; Slower = better quality")
        self.preset_combo.currentTextChanged.connect(self.update_estimate)  # Update estimates on preset change
        layout.addWidget(preset_label)
        layout.addWidget(self.preset_combo)

        # Bitrate
        bitrate_label = QLabel("Bitrate (kbps):")
        self.bitrate_edit = QLineEdit("2000")
        self.bitrate_edit.setToolTip("Target video bitrate; higher = better quality, larger files")
        self.bitrate_edit.textChanged.connect(self.update_estimate)
        layout.addWidget(bitrate_label)
        layout.addWidget(self.bitrate_edit)

        # Estimated Size
        self.estimate_label = QLabel("Estimated Total Size: N/A")
        layout.addWidget(self.estimate_label)

        # Estimated Time
        self.estimate_time_label = QLabel("Estimated Time: N/A")
        layout.addWidget(self.estimate_time_label)

        # Output Folder
        output_btn = QPushButton("Select Custom Output Folder")
        output_btn.clicked.connect(self.select_output_dir)
        layout.addWidget(output_btn)
        self.output_label = QLabel("Output: Same as input")
        layout.addWidget(self.output_label)

        # Start/Cancel Buttons
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("Start Conversion")
        self.start_btn.clicked.connect(self.start_conversion)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_conversion)
        self.cancel_btn.setEnabled(False)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        # Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)

        # Log Area
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text)

    def add_dropped_files(self, files):
        new_files = [f for f in files if f not in self.files]
        self.files.extend(new_files)
        self.update_file_list()
        self.update_estimate()
        self.log_message(f"Added {len(new_files)} dropped files.", "blue")

    def select_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Videos", "", "Video Files (*.mp4 *.mkv *.avi *.mov)")
        new_files = [f for f in files if f not in self.files]
        self.files.extend(new_files)
        self.update_file_list()
        self.update_estimate()

    def clear_list(self):
        self.files.clear()
        self.update_file_list()
        self.update_estimate()

    def remove_file(self, position):
        item = self.file_list.itemAt(position)
        if item:
            index = self.file_list.row(item)
            del self.files[index]
            self.update_file_list()
            self.update_estimate()

    def update_file_list(self):
        self.file_list.clear()
        for file in self.files:
            self.file_list.addItem(file)

    def select_output_dir(self):
        self.custom_output_dir = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if self.custom_output_dir:
            self.output_label.setText(f"Output: {self.custom_output_dir}")
        else:
            self.output_label.setText("Output: Same as input")

    def update_estimate(self):
        if not self.files:
            self.estimate_label.setText("Estimated Total Size: N/A")
            self.estimate_time_label.setText("Estimated Time: N/A")
            return
        total_size_mb = 0
        total_time_sec = 0
        bitrate = self.get_bitrate()
        preset = self.preset_combo.currentText()
        codec = self.get_codec()
        has_nvenc = ConversionThread.check_nvenc(self)  # Static call to check NVENC
        if not bitrate:
            return
        for file in self.files:
            try:
                probe = ffmpeg.probe(file)
                duration = float(probe['format']['duration'])
                audio_bitrate = next((float(s['bit_rate']) for s in probe['streams'] if s['codec_type'] == 'audio'), 128000) / 1000  # Default 128kbps
                
                # Size estimate
                video_size_kb = bitrate * duration
                audio_size_kb = (audio_bitrate * duration)
                total_size_mb += ((video_size_kb + audio_size_kb) / 8192) * 1.1  # 10% overhead, in MB
                
                # Time estimate
                preset_factor = self.get_preset_time_factor(preset)
                codec_factor = 1.5 if codec == "AV1" else 1.0  # AV1 slower
                hardware_factor = 0.33 if has_nvenc else 1.0  # NVENC ~3x faster
                file_time_sec = duration * preset_factor * codec_factor * hardware_factor
                total_time_sec += file_time_sec
            except Exception as e:
                self.log_message(f"Error probing {file}: {e}", "red")
        self.estimate_label.setText(f"Estimated Total Size: {total_size_mb:.2f} MB")
        total_time_min = total_time_sec / 60
        self.estimate_time_label.setText(f"Estimated Time: {total_time_min:.1f} min")

    def get_preset_time_factor(self, preset):
        # Rough multipliers: time relative to video duration (tune as needed)
        factors = {
            'ultrafast': 0.5,
            'superfast': 0.7,
            'veryfast': 0.8,
            'faster': 1.0,
            'fast': 1.2,
            'medium': 1.5,
            'slow': 2.5,
            'slower': 4.0,
            'veryslow': 8.0
        }
        return factors.get(preset, 1.5)  # Default medium

    def get_codec(self):
        return "AV1" if self.codec_av1.isChecked() else "H.265"

    def get_container(self):
        return "MP4" if self.container_mp4.isChecked() else "MKV"

    def get_bitrate(self):
        try:
            return int(self.bitrate_edit.text())
        except ValueError:
            QMessageBox.warning(self, "Invalid Input", "Bitrate must be a positive integer.")
            return None

    def start_conversion(self):
        if self.conversion_thread and self.conversion_thread.isRunning():
            return
        bitrate = self.get_bitrate()
        if not self.files or not bitrate:
            QMessageBox.warning(self, "Error", "Select files and enter a valid bitrate.")
            return

        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.status_bar.showMessage("Converting...")

        self.conversion_thread = ConversionThread(
            self.files, self.get_codec(), self.get_container(), str(bitrate), 
            self.preset_combo.currentText(), self.custom_output_dir
        )
        self.conversion_thread.progress_updated.connect(self.update_progress)
        self.conversion_thread.log_message.connect(self.log_message)
        self.conversion_thread.finished.connect(self.conversion_finished)
        self.conversion_thread.start()

    def cancel_conversion(self):
        if self.conversion_thread and self.conversion_thread.isRunning():
            self.conversion_thread.stop()
            self.conversion_thread.wait()  # Wait for thread to fully stop
            self.log_message("Conversion cancelled by user.", "orange")
            self.conversion_finished()

    def update_progress(self, value):
        int_value = int(value)  # Ensure int
        self.progress_bar.setValue(int_value)
        self.log_message(f"GUI progress set to {int_value}%", "gray")  # Debug: Confirm GUI update; remove after testing

    def log_message(self, message, color="black"):
        logging.info(message)
        self.log_text.append(f'<span style="color:{color};">{message}</span>')
        self.log_text.ensureCursorVisible()

    def conversion_finished(self):
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.status_bar.showMessage("Ready")
        self.conversion_thread = None  # Clear reference to allow new conversions
        QMessageBox.information(self, "Done", "Conversion completed or cancelled!")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoConverterApp()
    window.show()
    sys.exit(app.exec())