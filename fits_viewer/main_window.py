from __future__ import annotations

import os

from PySide6.QtCore import Qt, QSize, QThreadPool, QDir, QEvent, QSettings
from PySide6.QtGui import QImage, QPixmap, QIcon, QAction
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QTreeView, QListWidget, QListWidgetItem,
    QFileSystemModel, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QStatusBar, QFileDialog, QToolBar, QSlider,
    QMessageBox, QMenu,
)
import numpy as np
from PIL import Image

from .fits_utils import HEADER_FIELDS, FitsImageInfo
from .thumbnail_worker import ThumbnailWorker
from .image_viewer import EnlargeDialog

FITS_EXTENSIONS = {".fits", ".fit", ".fts"}
THUMB_SIZE = 220
MIN_THUMB_SIZE = 80
MAX_THUMB_SIZE = 400
ENLARGE_SIZE = 1600  # long-edge px for the spacebar full-size view
EXPORT_SIZE = 0  # 0 = full native resolution, no downsampling (Save Image As...)


def _np_gray_to_pixmap(arr: np.ndarray) -> QPixmap:
    h, w = arr.shape
    contiguous = np.ascontiguousarray(arr)
    image = QImage(contiguous.data, w, h, w, QImage.Format_Grayscale8)
    # Copy — the underlying numpy buffer goes out of scope otherwise.
    return QPixmap.fromImage(image.copy())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FITS Viewer")
        self.resize(1280, 800)

        self.thread_pool = QThreadPool.globalInstance()
        self._pending_workers = {}  # path -> worker, so we can tell stale results apart
        self._current_dir = None
        self._info_by_path: dict[str, FitsImageInfo] = {}
        self._pixmap_by_path: dict[str, QPixmap] = {}  # full-res thumb pixmap, rescaled for display

        self._enlarge_dialog: EnlargeDialog | None = None
        self._enlarge_path: str | None = None

        self._export_path: str | None = None

        self.settings = QSettings("NicoleJenkins", "FITSViewer")
        self._favorites: list[str] = self.settings.value("favorites", [], type=list)

        self._build_toolbar()
        self._build_layout()

    # ---------------------------------------------------------------- UI setup
    def _build_toolbar(self):
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setStyleSheet("""
            QToolBar {
                padding: 8px;
                spacing: 12px;
            }
            QToolButton {
                font-size: 14px;
                padding: 8px 14px;
            }
            QLabel {
                font-size: 14px;
            }
            QSlider::groove:horizontal {
                height: 6px;
            }
            QSlider::handle:horizontal {
                width: 20px;
                height: 20px;
                margin: -7px 0;
                border-radius: 10px;
            }
        """)
        self.addToolBar(toolbar)
        open_action = QAction("Open Folder...", self)
        open_action.triggered.connect(self._choose_root_folder)
        toolbar.addAction(open_action)

        self.add_favorite_action = QAction("Add to Favourites", self)
        self.add_favorite_action.triggered.connect(self._add_current_folder_favorite)
        self.add_favorite_action.setEnabled(False)  # enabled once a folder is open
        toolbar.addAction(self.add_favorite_action)

        self.export_action = QAction("Export Image...", self)
        self.export_action.triggered.connect(self._export_selected_image)
        self.export_action.setEnabled(False)  # enabled once a file is selected
        toolbar.addAction(self.export_action)

        toolbar.addSeparator()
        toolbar.addWidget(QLabel("  Thumbnail size:  "))
        self.thumb_size_slider = QSlider(Qt.Horizontal)
        self.thumb_size_slider.setMinimum(MIN_THUMB_SIZE)
        self.thumb_size_slider.setMaximum(MAX_THUMB_SIZE)
        self.thumb_size_slider.setValue(THUMB_SIZE)
        self.thumb_size_slider.setFixedWidth(180)
        self.thumb_size_slider.valueChanged.connect(self._on_thumb_size_changed)
        toolbar.addWidget(self.thumb_size_slider)

    def _build_layout(self):
        splitter = QSplitter(Qt.Horizontal)

        # --- Favourites + folder tree (left) ---
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        favorites_label = QLabel("FAVOURITES")
        favorites_label.setStyleSheet("font-weight: 600; color: #888; padding: 4px 6px;")
        left_layout.addWidget(favorites_label)

        self.favorites_list = QListWidget()
        self.favorites_list.setMaximumHeight(140)
        self.favorites_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.favorites_list.customContextMenuRequested.connect(self._show_favorites_context_menu)
        self.favorites_list.itemClicked.connect(self._on_favorite_clicked)
        left_layout.addWidget(self.favorites_list)
        self._refresh_favorites_list()

        folders_label = QLabel("FOLDERS")
        folders_label.setStyleSheet("font-weight: 600; color: #888; padding: 4px 6px;")
        left_layout.addWidget(folders_label)

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
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)
        left_layout.addWidget(self.tree)

        splitter.addWidget(left)

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
            self._navigate_tree_to(folder)
            self._load_folder(folder)

    def _navigate_tree_to(self, path: str):
        """Select and reveal a folder in the tree without hiding its
        parents/drives above it - setRootIndex() was used here previously,
        which scoped the tree down to only that folder's subfolders and
        made the tree look empty for any folder with no subdirectories."""
        idx = self.fs_model.index(path)
        self.tree.setCurrentIndex(idx)
        self.tree.scrollTo(idx)
        self.tree.expand(idx)

    def _on_tree_selected(self, index):
        path = self.fs_model.filePath(index)
        if os.path.isdir(path):
            self._load_folder(path)

    # ---------------------------------------------------------------- favourites
    def _show_tree_context_menu(self, pos):
        index = self.tree.indexAt(pos)
        if not index.isValid():
            return
        path = self.fs_model.filePath(index)
        if not os.path.isdir(path):
            return

        menu = QMenu(self)
        if path in self._favorites:
            action = menu.addAction("Already in Favourites")
            action.setEnabled(False)
        else:
            action = menu.addAction("Add to Favourites")
            action.triggered.connect(lambda: self._add_favorite(path))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _show_favorites_context_menu(self, pos):
        item = self.favorites_list.itemAt(pos)
        if item is None:
            return
        path = item.data(Qt.UserRole)

        menu = QMenu(self)
        remove_action = menu.addAction("Remove from Favourites")
        remove_action.triggered.connect(lambda: self._remove_favorite(path))
        menu.exec(self.favorites_list.viewport().mapToGlobal(pos))

    def _add_current_folder_favorite(self):
        if self._current_dir is None:
            return
        self._add_favorite(self._current_dir)
        self.statusBar().showMessage(f"Added to favourites: {self._current_dir}")

    def _add_favorite(self, path: str):
        if path in self._favorites:
            return
        self._favorites.append(path)
        self._save_favorites()
        self._refresh_favorites_list()

    def _remove_favorite(self, path: str):
        if path not in self._favorites:
            return
        self._favorites.remove(path)
        self._save_favorites()
        self._refresh_favorites_list()

    def _save_favorites(self):
        self.settings.setValue("favorites", self._favorites)

    def _refresh_favorites_list(self):
        self.favorites_list.clear()
        for path in self._favorites:
            name = os.path.basename(path.rstrip("/\\")) or path
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, path)
            item.setToolTip(path)
            self.favorites_list.addItem(item)

    def _on_favorite_clicked(self, item: QListWidgetItem):
        path = item.data(Qt.UserRole)
        if not os.path.isdir(path):
            self.statusBar().showMessage(f"Folder no longer exists: {path}")
            return
        self._navigate_tree_to(path)
        self._load_folder(path)

    def _load_folder(self, folder: str):
        self._current_dir = folder
        self.add_favorite_action.setEnabled(True)
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

    # ---------------------------------------------------------------- export
    def _export_selected_image(self):
        item = self.grid.currentItem()
        if item is None:
            return
        path = item.data(Qt.UserRole)

        self._export_path = path
        self.statusBar().showMessage(f"Decoding {os.path.basename(path)} at full resolution...")

        worker = ThumbnailWorker(path, thumb_size=EXPORT_SIZE)
        worker.signals.finished.connect(self._on_export_ready)
        self.thread_pool.start(worker)

    def _on_export_ready(self, path: str, thumb, info: FitsImageInfo):
        # User may have selected a different file while this was decoding.
        if path != self._export_path:
            return
        self._export_path = None

        if thumb is None:
            self.statusBar().showMessage("Export failed")
            QMessageBox.warning(
                self, "Export failed",
                f"Could not decode {os.path.basename(path)}: {info.error or 'unknown error'}",
            )
            return

        default_name = os.path.splitext(os.path.basename(path))[0] + ".png"
        save_path, _filter = QFileDialog.getSaveFileName(
            self, "Export Image", default_name,
            "PNG Image (*.png);;JPEG Image (*.jpg);;TIFF Image (*.tiff)",
        )
        if not save_path:
            self.statusBar().showMessage("Export cancelled")
            return

        try:
            Image.fromarray(thumb, mode="L").save(save_path)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not a crash
            QMessageBox.warning(self, "Export failed", f"Could not save file: {exc}")
            self.statusBar().showMessage("Export failed")
            return

        self.statusBar().showMessage(f"Saved {save_path}")

    def _on_thumbnail_selected(self, current: QListWidgetItem, _previous):
        if current is None:
            self.filename_label.setText("No file selected")
            for row in range(self.header_table.rowCount()):
                self.header_table.setItem(row, 1, QTableWidgetItem(""))
            self.export_action.setEnabled(False)
            return

        path = current.data(Qt.UserRole)
        self.filename_label.setText(os.path.basename(path))
        self.export_action.setEnabled(True)
        info = self._info_by_path.get(path)
        if info is None:
            return

        for row, (key, _label) in enumerate(HEADER_FIELDS):
            value = info.header.get(key, "-")
            self.header_table.setItem(row, 1, QTableWidgetItem(str(value)))
