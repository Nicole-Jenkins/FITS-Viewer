from __future__ import annotations

import os

from PySide6.QtCore import Qt, QSize, QThreadPool, QDir, QEvent
from PySide6.QtGui import QImage, QPixmap, QIcon
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QTreeView, QListWidget, QListWidgetItem,
    QFileSystemModel, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QStatusBar, QFileDialog, QToolBar, QSlider,
)
from PySide6.QtGui import QAction
import numpy as np

from .fits_utils import HEADER_FIELDS, FitsImageInfo
from .thumbnail_worker import ThumbnailWorker
from .image_viewer import EnlargeDialog

FITS_EXTENSIONS = {".fits", ".fit", ".fts"}
THUMB_SIZE = 220
MIN_THUMB_SIZE = 80
MAX_THUMB_SIZE = 400
ENLARGE_SIZE = 1600  # long-edge px for the spacebar full-size view


def _np_gray_to_pixmap(arr: np.ndarray) -> QPixmap:
    h, w = arr.shape
    contiguous = np.ascontiguousarray(arr)
    image = QImage(contiguous.data, w, h, w, QImage.Format_Grayscale8)
    # Copy — the underlying numpy buffer goes out of scope otherwise.
    return QPixmap.fromImage(image.copy())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FITS Bridge")
        self.resize(1280, 800)

        self.thread_pool = QThreadPool.globalInstance()
        self._pending_workers = {}  # path -> worker, so we can tell stale results apart
        self._current_dir = None
        self._info_by_path: dict[str, FitsImageInfo] = {}
        self._pixmap_by_path: dict[str, QPixmap] = {}  # full-res thumb pixmap, rescaled for display

        self._enlarge_dialog: EnlargeDialog | None = None
        self._enlarge_path: str | None = None

        self._build_toolbar()
        self._build_layout()

    # ---------------------------------------------------------------- UI setup
    def _build_toolbar(self):
        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)
        open_action = QAction("Open Folder...", self)
        open_action.triggered.connect(self._choose_root_folder)
        toolbar.addAction(open_action)

        toolbar.addSeparator()
        toolbar.addWidget(QLabel("Thumbnail size:  "))
        self.thumb_size_slider = QSlider(Qt.Horizontal)
        self.thumb_size_slider.setMinimum(MIN_THUMB_SIZE)
        self.thumb_size_slider.setMaximum(MAX_THUMB_SIZE)
        self.thumb_size_slider.setValue(THUMB_SIZE)
        self.thumb_size_slider.setFixedWidth(140)
        self.thumb_size_slider.valueChanged.connect(self._on_thumb_size_changed)
        toolbar.addWidget(self.thumb_size_slider)

    def _build_layout(self):
        splitter = QSplitter(Qt.Horizontal)

        # --- Folder tree (left) ---
        self.fs_model = QFileSystemModel()
        self.fs_model.setRootPath("")
        self.fs_model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot)

        self.tree = QTreeView()
        self.tree.setModel(self.fs_model)
        self.tree.setColumnHidden(1, True)
        self.tree.setColumnHidden(2, True)
        self.tree.setColumnHidden(3, True)
        self.tree.setHeaderHidden(True)
        self.tree.clicked.connect(self._on_tree_selected)
        splitter.addWidget(self.tree)

        # --- Thumbnail grid (middle) ---
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setSpacing(10)
        self.grid.setWordWrap(True)
        self.grid.setMovement(QListWidget.Static)
        self.grid.currentItemChanged.connect(self._on_thumbnail_selected)
        self.grid.installEventFilter(self)
        splitter.addWidget(self.grid)

        # --- Header panel (right) ---
        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.filename_label = QLabel("No file selected")
        self.filename_label.setWordWrap(True)
        self.filename_label.setStyleSheet("font-weight: 600; font-size: 13px;")
        right_layout.addWidget(self.filename_label)

        self.header_table = QTableWidget(len(HEADER_FIELDS), 2)
        self.header_table.setHorizontalHeaderLabels(["Field", "Value"])
        self.header_table.verticalHeader().setVisible(False)
        self.header_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.header_table.setEditTriggers(QTableWidget.NoEditTriggers)
        for row, (_key, label) in enumerate(HEADER_FIELDS):
            self.header_table.setItem(row, 0, QTableWidgetItem(label))
            self.header_table.setItem(row, 1, QTableWidgetItem(""))
        right_layout.addWidget(self.header_table)
        right.setMaximumWidth(360)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 1)

        self.setCentralWidget(splitter)
        self.setStatusBar(QStatusBar())

    # ---------------------------------------------------------------- actions
    def _choose_root_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose astro library folder")
        if folder:
            idx = self.fs_model.index(folder)
            self.tree.setRootIndex(idx)
            self.tree.expand(idx)
            self._load_folder(folder)

    def _on_tree_selected(self, index):
        path = self.fs_model.filePath(index)
        if os.path.isdir(path):
            self._load_folder(path)

    def _load_folder(self, folder: str):
        self._current_dir = folder
        self.grid.clear()
        self._info_by_path.clear()
        self.statusBar().showMessage(f"Scanning {folder}...")

        try:
            entries = sorted(os.listdir(folder))
        except OSError as exc:
            self.statusBar().showMessage(f"Could not open folder: {exc}")
            return

        fits_files = [
            os.path.join(folder, name)
            for name in entries
            if os.path.splitext(name)[1].lower() in FITS_EXTENSIONS
        ]

        if not fits_files:
            self.statusBar().showMessage(f"No FITS files in {folder}")
            return

        for path in fits_files:
            item = QListWidgetItem(os.path.basename(path))
            item.setData(Qt.UserRole, path)
            item.setSizeHint(QSize(THUMB_SIZE + 20, THUMB_SIZE + 40))
            self.grid.addItem(item)
            self._start_thumbnail_job(path)

        self.statusBar().showMessage(f"{len(fits_files)} FITS file(s) in {folder}")

    def _start_thumbnail_job(self, path: str):
        worker = ThumbnailWorker(path, thumb_size=THUMB_SIZE)
        worker.signals.finished.connect(self._on_thumbnail_ready)
        self._pending_workers[path] = worker
        self.thread_pool.start(worker)

    def _on_thumbnail_ready(self, path: str, thumb, info: FitsImageInfo):
        # Ignore results for a folder we've since navigated away from.
        if self._current_dir is None or not path.startswith(self._current_dir):
            return
        self._info_by_path[path] = info

        item = self._find_item_for_path(path)
        if item is None:
            return

        if thumb is not None:
            pixmap = _np_gray_to_pixmap(thumb)
            self._pixmap_by_path[path] = pixmap
            item.setIcon(QIcon(self._scaled_pixmap(pixmap)))
        item.setText(f"{os.path.basename(path)}\n{info.display_object()} · {info.display_filter()}")
        if info.error:
            item.setToolTip(f"Error reading file: {info.error}")

    def _find_item_for_path(self, path: str):
        for i in range(self.grid.count()):
            item = self.grid.item(i)
            if item.data(Qt.UserRole) == path:
                return item
        return None

    # ---------------------------------------------------------------- thumbnail size
    def _scaled_pixmap(self, pixmap: QPixmap) -> QPixmap:
        """Rescale a decoded thumbnail to the current slider size.

        Note: thumbnails are decoded once at THUMB_SIZE (220px). Sliding
        above that just upscales this pixmap and will look soft - it's a
        cheap in-memory resize, not a re-decode. Re-decoding at every
        slider tick would mean hammering the thread pool on every drag
        event, so this trades a bit of sharpness at the high end for a
        slider that stays responsive.
        """
        size = self.grid.iconSize()
        if pixmap.width() == size.width() and pixmap.height() == size.height():
            return pixmap
        return pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _on_thumb_size_changed(self, value: int):
        self.grid.setIconSize(QSize(value, value))
        for i in range(self.grid.count()):
            item = self.grid.item(i)
            path = item.data(Qt.UserRole)
            pixmap = self._pixmap_by_path.get(path)
            if pixmap is not None:
                item.setIcon(QIcon(self._scaled_pixmap(pixmap)))
            item.setSizeHint(QSize(value + 20, value + 40))

    # ---------------------------------------------------------------- enlarge view
    def eventFilter(self, obj, event):
        if (
            obj is self.grid
            and event.type() == QEvent.KeyPress
            and event.key() == Qt.Key_Space
        ):
            self._open_enlarge_view()
            return True
        return super().eventFilter(obj, event)

    def _open_enlarge_view(self):
        item = self.grid.currentItem()
        if item is None:
            return
        path = item.data(Qt.UserRole)

        dialog = EnlargeDialog(os.path.basename(path), parent=self)
        dialog.setModal(False)
        dialog.finished.connect(self._on_enlarge_dialog_closed)
        self._enlarge_dialog = dialog
        self._enlarge_path = path
        dialog.show()

        worker = ThumbnailWorker(path, thumb_size=ENLARGE_SIZE)
        worker.signals.finished.connect(self._on_enlarge_ready)
        self.thread_pool.start(worker)

    def _on_enlarge_dialog_closed(self, _result):
        self._enlarge_dialog = None
        self._enlarge_path = None

    def _on_enlarge_ready(self, path: str, thumb, info: FitsImageInfo):
        # Dialog may have been closed, or a different item opened, before
        # this background decode finished - discard stale results the same
        # way the grid already does for regular thumbnails.
        if self._enlarge_dialog is None or path != self._enlarge_path:
            return
        if thumb is None:
            self._enlarge_dialog.set_error(info.error or "Could not decode image")
            return
        self._enlarge_dialog.set_pixmap(_np_gray_to_pixmap(thumb))

    def _on_thumbnail_selected(self, current: QListWidgetItem, _previous):
        if current is None:
            self.filename_label.setText("No file selected")
            for row in range(self.header_table.rowCount()):
                self.header_table.setItem(row, 1, QTableWidgetItem(""))
            return

        path = current.data(Qt.UserRole)
        self.filename_label.setText(os.path.basename(path))
        info = self._info_by_path.get(path)
        if info is None:
            return

        for row, (key, _label) in enumerate(HEADER_FIELDS):
            value = info.header.get(key, "-")
            self.header_table.setItem(row, 1, QTableWidgetItem(str(value)))
