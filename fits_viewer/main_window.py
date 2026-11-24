from __future__ import annotations

import os
import shutil
import sys

from PySide6.QtCore import Qt, QSize, QThreadPool, QDir, QEvent, QSettings, QUrl, QRectF
from PySide6.QtGui import (
    QImage, QPixmap, QIcon, QAction, QDesktopServices, QColor, QBrush, QPainter,
    QPainterPath, QPen, QKeySequence,
)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QTreeView, QListWidget, QListWidgetItem,
    QFileSystemModel, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QStatusBar, QFileDialog, QToolBar, QSlider,
    QMessageBox, QMenu, QApplication,
)
import numpy as np
from PIL import Image
from send2trash import send2trash

from .fits_utils import HEADER_FIELDS, FitsImageInfo, is_fits_filename
from .thumbnail_worker import ThumbnailWorker, FolderPreviewWorker, is_hidden_or_system_folder
from .image_viewer import EnlargeDialog
from .update_checker import UpdateCheckWorker
from .version import APP_VERSION

THUMB_SIZE = 220
MIN_THUMB_SIZE = 80
MAX_THUMB_SIZE = 400
ENLARGE_SIZE = 1600  # long-edge px for the spacebar full-size view
EXPORT_SIZE = 0  # 0 = full native resolution, no downsampling (Save Image As...)


def _clean_path(path: str) -> str:
    """Normalises a path to the operating system's own form.

    Qt hands back paths with forward slashes (C:/Users/...), and
    os.path.join then adds a backslash before the filename, giving a mixed
    path. Most of Windows tolerates that, but send2trash adds the \\\\?\\
    long-path prefix, which turns off Windows' slash tidying - so a mixed
    path "doesn't exist" and deletion fails with Errno 3. Normalising every
    path as it enters the app avoids that (and keeps path comparisons
    consistent)."""
    return os.path.normpath(path)


def _same_path(a: str, b: str) -> bool:
    """True if two paths point at the same place, ignoring slash style and
    (on Windows) letter case."""
    return os.path.normcase(_clean_path(a)) == os.path.normcase(_clean_path(b))


def _is_fits_file(name: str) -> bool:
    return is_fits_filename(name)


# Grid items are either FITS files or subfolders. The item's path lives in
# Qt.UserRole (as before); ROLE_KIND marks folder tiles, so everything that
# only makes sense for files (header panel, popup, export, marking for
# deletion) can skip them.
ROLE_KIND = Qt.UserRole + 1
KIND_FOLDER = "folder"

FOLDER_COLOUR = QColor(214, 170, 76)       # folder body
FOLDER_TAB_COLOUR = QColor(184, 140, 52)   # folder tab / outline


def _paint_folder_shape(painter: QPainter, rect: QRectF) -> None:
    """Draws a simple folder icon filling rect (tab top-left, body below)."""
    w, h = rect.width(), rect.height()
    x, y = rect.x(), rect.y()
    tab = QPainterPath()
    tab.addRoundedRect(QRectF(x, y, w * 0.42, h * 0.3), h * 0.06, h * 0.06)
    painter.fillPath(tab, FOLDER_TAB_COLOUR)
    body = QPainterPath()
    body.addRoundedRect(QRectF(x, y + h * 0.14, w, h * 0.86), h * 0.07, h * 0.07)
    painter.fillPath(body, FOLDER_COLOUR)
    painter.setPen(QPen(FOLDER_TAB_COLOUR, max(1.0, h * 0.02)))
    painter.drawPath(body)


def _folder_placeholder_pixmap(size: int) -> QPixmap:
    """A plain folder picture for folder tiles with no image to preview
    (or while the preview is still being made)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    _paint_folder_shape(painter, QRectF(size * 0.12, size * 0.2, size * 0.76, size * 0.6))
    painter.end()
    return pixmap


def _with_folder_badge(preview: QPixmap) -> QPixmap:
    """Adds a small folder badge in the bottom-left corner of a preview
    image, so folder tiles can't be mistaken for images."""
    result = QPixmap(preview)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing)
    badge = max(16.0, min(result.width(), result.height()) * 0.26)
    margin = badge * 0.18
    rect = QRectF(margin, result.height() - badge * 0.8 - margin, badge, badge * 0.8)
    _paint_folder_shape(painter, rect)
    painter.end()
    return result


def _is_folder_item(item: QListWidgetItem | None) -> bool:
    return item is not None and item.data(ROLE_KIND) == KIND_FOLDER


def _format_header_value(value) -> str:
    """Shows header numbers the way a person would write them. Some
    software (e.g. Seestar) works in lower, 32-bit precision and writes
    numbers like 2.9 into the header as 2.90000009536743. If a number is
    the same as a 32-bit value to within rounding (1 part in a billion),
    it's printed at 32-bit precision (2.9). Anything else (RA/Dec etc)
    differs by far more than that, so it's printed in full, untouched."""
    if isinstance(value, float) and np.isfinite(value) and value != 0.0:
        as32 = np.float32(value)
        if abs(float(as32) - value) <= abs(value) * 1e-9:
            return str(as32)
    return str(value)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _is_inside(path: str, folder: str) -> bool:
    """True if path is folder itself or anywhere inside it."""
    p = os.path.normcase(_clean_path(path))
    f = os.path.normcase(_clean_path(folder))
    return p == f or p.startswith(f.rstrip(os.sep) + os.sep)


def _folder_contents(folder: str):
    """Counts everything inside a folder (all levels down) for the delete
    confirmation: (fits_files, other_files, subfolders, total_bytes)."""
    fits_count = other_count = sub_count = total = 0
    for root, dirs, files in os.walk(folder):
        sub_count += len(dirs)
        for name in files:
            if _is_fits_file(name):
                fits_count += 1
            else:
                other_count += 1
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return fits_count, other_count, sub_count, total


# Windows' Recycle Bin has a size limit per drive (by default a few
# percent of the drive - commonly around 5-10%). Anything bigger than the
# limit, or anything on a network drive, can be deleted *permanently*
# instead of recycled. We can't cheaply read the exact limit, so we warn
# whenever an item is bigger than 5% of its drive - erring towards warning.
RECYCLE_BIN_FRACTION = 0.05


def _is_network_path(path: str) -> bool:
    if sys.platform != "win32":
        return False
    if path.startswith("\\\\"):
        return True
    try:
        import ctypes
        drive = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
        return ctypes.windll.kernel32.GetDriveTypeW(drive) == 4  # DRIVE_REMOTE
    except Exception:  # noqa: BLE001 - unknown means "don't warn about this part"
        return False


def _may_skip_recycle_bin(path: str, size: int) -> str | None:
    """Returns a reason string if Windows might delete this permanently
    instead of putting it in the Recycle Bin, else None."""
    if sys.platform != "win32":
        return None
    if _is_network_path(path):
        return "is on a network drive"
    try:
        drive_total = shutil.disk_usage(path).total
    except OSError:
        return None
    if size > drive_total * RECYCLE_BIN_FRACTION:
        return f"is {_format_size(size)}, which may be too big for the Recycle Bin"
    return None


def _np_gray_to_pixmap(arr: np.ndarray) -> QPixmap:
    contiguous = np.ascontiguousarray(arr)
    if contiguous.ndim == 3:
        h, w, _ = contiguous.shape
        image = QImage(contiguous.data, w, h, w * 3, QImage.Format_RGB888)
    else:
        h, w = contiguous.shape
        image = QImage(contiguous.data, w, h, w, QImage.Format_Grayscale8)
    # Copy — the underlying numpy buffer goes out of scope otherwise.
    return QPixmap.fromImage(image.copy())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FITS Viewer")
        self.resize(1280, 800)

        self.thread_pool = QThreadPool.globalInstance()
        self._pending_workers = {}  # path -> worker, used to identify stale results
        self._current_dir = None
        self._info_by_path: dict[str, FitsImageInfo] = {}
        self._pixmap_by_path: dict[str, QPixmap] = {}  # full-res thumb pixmap, rescaled for display

        # Every open full-size popup. Several can be open at once (handy for
        # comparing subs side by side); each one remembers which file it is
        # showing in its own .path, so keys pressed in a popup always act on
        # *that* popup's image.
        self._enlarge_dialogs: list[EnlargeDialog] = []

        self._export_path: str | None = None

        self.settings = QSettings("NicoleJenkins", "FITSViewer")
        self._favorites: list[str] = self.settings.value("favorites", [], type=list)

        self._marked_for_deletion: set[str] = set()

        self._build_toolbar()
        self._build_layout()
        self._start_update_check()

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

        self.up_action = QAction("Up", self)
        self.up_action.setToolTip("Go to the folder above this one (Alt+Up)")
        self.up_action.setShortcut(QKeySequence("Alt+Up"))
        self.up_action.triggered.connect(self._go_up_folder)
        self.up_action.setEnabled(False)  # enabled once a folder is open
        toolbar.addAction(self.up_action)

        self.add_favorite_action = QAction("Add to Favourites", self)
        self.add_favorite_action.triggered.connect(self._add_current_folder_favorite)
        self.add_favorite_action.setEnabled(False)  # enabled once a folder is open
        toolbar.addAction(self.add_favorite_action)

        self.export_action = QAction("Export Image...", self)
        self.export_action.triggered.connect(self._export_selected_image)
        self.export_action.setEnabled(False)  # enabled once a file is selected
        toolbar.addAction(self.export_action)

        self.delete_marked_action = QAction("Delete Marked", self)
        self.delete_marked_action.setToolTip(
            "Send everything marked (files and folders) to the Recycle Bin")
        self.delete_marked_action.triggered.connect(self._delete_marked_files)
        self.delete_marked_action.setEnabled(False)  # enabled once something is marked
        toolbar.addAction(self.delete_marked_action)

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

        # --- Current folder path + thumbnail grid (middle) ---
        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(4)
        self.path_label = QLabel("No folder open")
        self.path_label.setStyleSheet("color: #aaa; padding: 4px 6px;")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        middle_layout.addWidget(self.path_label)

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setSpacing(10)
        self.grid.setWordWrap(True)
        self.grid.setMovement(QListWidget.Static)
        # Several images can be selected at once, like File Explorer:
        # Ctrl+click to add/remove one, Shift+click for a run of them.
        self.grid.setSelectionMode(QListWidget.ExtendedSelection)
        self.grid.currentItemChanged.connect(self._on_thumbnail_selected)
        self.grid.installEventFilter(self)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._show_grid_context_menu)
        # Double-click or Enter. Folder tiles open the folder; for image
        # files this is where "Open in editor" will go later.
        self.grid.itemActivated.connect(self._on_grid_item_activated)
        middle_layout.addWidget(self.grid)
        splitter.addWidget(middle)

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
        """Selects and reveals a folder without restricting the tree's
        root, keeping parent directories/drives visible. Avoids
        setRootIndex(), which scopes the view to only the given folder's
        subdirectories and renders empty for folders with none."""
        idx = self.fs_model.index(QDir.fromNativeSeparators(path))
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
        if any(_same_path(path, fav) for fav in self._favorites):
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
        if any(_same_path(path, fav) for fav in self._favorites):
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
        folder = _clean_path(folder)
        self._current_dir = folder
        self.add_favorite_action.setEnabled(True)
        parent = os.path.dirname(folder)
        self.up_action.setEnabled(bool(parent) and not _same_path(parent, folder))
        self.path_label.setText(folder)
        self.path_label.setToolTip(folder)
        self.grid.clear()
        self._info_by_path.clear()
        self._pixmap_by_path.clear()
        self._pending_workers.clear()
        self.statusBar().showMessage(f"Scanning {folder}...")
        current_thumb_size = self.thumb_size_slider.value()

        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError as exc:
            self.statusBar().showMessage(f"Could not open folder: {exc}")
            return

        subfolders = []
        fits_files = []
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if not is_hidden_or_system_folder(entry.name):
                    subfolders.append(os.path.join(folder, entry.name))
            elif _is_fits_file(entry.name):
                fits_files.append(os.path.join(folder, entry.name))

        tile_size = QSize(current_thumb_size + 20, current_thumb_size + 40)

        # Folder tiles first (like File Explorer / Adobe Bridge), then images.
        placeholder = _folder_placeholder_pixmap(self._thumb_decode_size())
        for path in subfolders:
            item = QListWidgetItem(f"{os.path.basename(path)}\nLooking inside...")
            item.setData(Qt.UserRole, path)
            item.setData(ROLE_KIND, KIND_FOLDER)
            item.setSizeHint(tile_size)
            item.setToolTip(f"{path}\nDouble-click to open")
            self._pixmap_by_path[path] = placeholder
            item.setIcon(QIcon(self._scaled_pixmap(placeholder)))
            self.grid.addItem(item)
            self._apply_mark_visual(item, path in self._marked_for_deletion)
            self._start_folder_preview_job(path)

        for path in fits_files:
            item = QListWidgetItem(os.path.basename(path))
            item.setData(Qt.UserRole, path)
            item.setSizeHint(tile_size)
            self.grid.addItem(item)
            self._apply_mark_visual(item, path in self._marked_for_deletion)
            self._start_thumbnail_job(path)

        if not subfolders and not fits_files:
            self.statusBar().showMessage(f"No FITS files or subfolders in {folder}")
            return
        parts = []
        if subfolders:
            parts.append(f"{len(subfolders)} folder{'s' if len(subfolders) != 1 else ''}")
        parts.append(f"{len(fits_files)} FITS file{'s' if len(fits_files) != 1 else ''}")
        self.statusBar().showMessage(f"{', '.join(parts)} in {folder}")

    def _start_folder_preview_job(self, folder: str):
        worker = FolderPreviewWorker(folder, thumb_size=self._thumb_decode_size())
        worker.signals.finished.connect(self._on_folder_preview_ready)
        self.thread_pool.start(worker)

    def _on_folder_preview_ready(self, folder: str, fits_count: int, sub_count: int, thumb):
        item = self._find_item_for_path(folder)
        if not _is_folder_item(item):
            return  # a different folder is open now
        details = []
        if fits_count:
            details.append(f"{fits_count} FITS file{'s' if fits_count != 1 else ''}")
        if sub_count:
            details.append(f"{sub_count} folder{'s' if sub_count != 1 else ''}")
        item.setText(f"{os.path.basename(folder)}\n{' · '.join(details) if details else 'No FITS files'}")
        if thumb is not None:
            pixmap = _with_folder_badge(_np_gray_to_pixmap(thumb))
            self._pixmap_by_path[folder] = pixmap
            item.setIcon(QIcon(self._scaled_pixmap(pixmap)))

    def _open_folder(self, folder: str):
        self._navigate_tree_to(folder)
        self._load_folder(folder)

    def _on_grid_item_activated(self, item: QListWidgetItem):
        if _is_folder_item(item):
            self._open_folder(item.data(Qt.UserRole))

    def _go_up_folder(self):
        if self._current_dir is None:
            return
        came_from = self._current_dir
        parent = os.path.dirname(came_from)
        if not parent or _same_path(parent, came_from):
            return
        self._open_folder(parent)
        # Select the folder we just came out of, so it's easy to see where
        # you were (and press Enter to go straight back in).
        item = self._find_item_for_path(came_from)
        if item is not None:
            self.grid.setCurrentItem(item)
            self.grid.scrollToItem(item)

    def _thumb_decode_size(self) -> int:
        """Thumbnails are made once, at the largest size the slider can go
        to, multiplied by the screen's scaling (e.g. 150% on many Windows
        laptops), so they're only ever scaled *down* for display - never
        stretched up, which made them look soft and crunchy."""
        ratio = max(1.0, float(self.devicePixelRatioF()))
        return int(round(MAX_THUMB_SIZE * ratio))

    def _start_thumbnail_job(self, path: str):
        worker = ThumbnailWorker(path, thumb_size=self._thumb_decode_size())
        worker.signals.finished.connect(self._on_thumbnail_ready)
        self._pending_workers[path] = worker
        self.thread_pool.start(worker)

    def _on_thumbnail_ready(self, path: str, thumb, info: FitsImageInfo):
        # Discards results for a folder that is no longer open.
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

    # ---------------------------------------------------------------- mark for deletion
    MARK_BACKGROUND = QColor(120, 30, 30)  # dark red, visible against the dark theme

    def _apply_mark_visual(self, item: QListWidgetItem, marked: bool):
        is_folder = _is_folder_item(item)
        if marked:
            item.setBackground(QBrush(self.MARK_BACKGROUND))
            item.setToolTip("Folder marked for deletion (with everything inside it)"
                            if is_folder else "Marked for deletion")
        else:
            item.setBackground(QBrush())  # reset to default
            item.setToolTip(f"{item.data(Qt.UserRole)}\nDouble-click to open" if is_folder else "")

    def _set_marked(self, path: str, marked: bool):
        if marked:
            self._marked_for_deletion.add(path)
        else:
            self._marked_for_deletion.discard(path)

        item = self._find_item_for_path(path)
        if item is not None:
            self._apply_mark_visual(item, marked)
        self._refresh_enlarge_marks(path)

        self._update_delete_action()

    def _update_delete_action(self):
        count = len(self._marked_for_deletion)
        self.delete_marked_action.setEnabled(count > 0)
        self.delete_marked_action.setText(f"Delete Marked ({count})" if count else "Delete Marked")

    def _selected_or_current_items(self) -> list[QListWidgetItem]:
        """The selected grid items in grid order, or just the current item
        if nothing is selected."""
        items = sorted(self.grid.selectedItems(), key=self.grid.row)
        if not items and self.grid.currentItem() is not None:
            items = [self.grid.currentItem()]
        return items

    def _toggle_mark_current_item(self):
        """Delete key: marks/unmarks everything selected. If any selected
        item is unmarked, all of them get marked; if they're all marked
        already, all of them get unmarked."""
        items = self._selected_or_current_items()
        if not items:
            return
        paths = [item.data(Qt.UserRole) for item in items]
        mark = any(p not in self._marked_for_deletion for p in paths)
        for path in paths:
            self._set_marked(path, mark)

    def _show_grid_context_menu(self, pos):
        item = self.grid.itemAt(pos)
        if item is None:
            return
        path = item.data(Qt.UserRole)
        if _is_folder_item(item):
            marked = path in self._marked_for_deletion
            menu = QMenu(self)
            menu.addAction("Open Folder").triggered.connect(lambda: self._open_folder(path))
            menu.addAction("Unmark for Deletion" if marked else "Mark for Deletion").triggered.connect(
                lambda: self._set_marked(path, not marked))
            if any(_same_path(path, fav) for fav in self._favorites):
                menu.addAction("Already in Favourites").setEnabled(False)
            else:
                menu.addAction("Add to Favourites").triggered.connect(lambda: self._add_favorite(path))
            menu.exec(self.grid.viewport().mapToGlobal(pos))
            return
        marked = path in self._marked_for_deletion

        menu = QMenu(self)
        action = menu.addAction("Unmark for Deletion" if marked else "Mark for Deletion")
        action.triggered.connect(lambda: self._set_marked(path, not marked))
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    def _delete_marked_files(self):
        # Anything marked that no longer exists (deleted outside the app)
        # is just unmarked.
        for path in [p for p in self._marked_for_deletion if not os.path.exists(p)]:
            self._marked_for_deletion.discard(path)
        marked = sorted(self._marked_for_deletion)
        if not marked:
            self._update_delete_action()
            return

        folders = [p for p in marked if os.path.isdir(p)]
        # Files (or folders) inside a marked folder go with that folder, so
        # they're not sent separately (or counted twice).
        top_level = [
            p for p in marked
            if not any(_is_inside(p, f) and not _same_path(p, f) for f in folders)
        ]
        top_folders = [p for p in top_level if os.path.isdir(p)]
        top_files = [p for p in top_level if not os.path.isdir(p)]

        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.statusBar().showMessage("Checking what's inside the marked folders...")
        try:
            folder_details = {f: _folder_contents(f) for f in top_folders}
        finally:
            QApplication.restoreOverrideCursor()

        # ---- build the confirmation message
        what = []
        if top_files:
            what.append(_plural(len(top_files), "file"))
        if top_folders:
            what.append(_plural(len(top_folders), "folder"))
        lines = [f"You're about to send {' and '.join(what)} to the Recycle Bin."]

        warnings = []
        if top_folders:
            lines.append("")
            lines.append("Folders (everything inside them goes too):")
            for folder in top_folders[:10]:
                fits_n, other_n, sub_n, size = folder_details[folder]
                parts = [_plural(fits_n, "FITS file")]
                if other_n:
                    parts.append(_plural(other_n, "other file"))
                if sub_n:
                    parts.append(_plural(sub_n, "subfolder"))
                contents = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
                lines.append(f"  \u2022 {os.path.basename(folder)} - contains {contents} ({_format_size(size)})")
                reason = _may_skip_recycle_bin(folder, size)
                if reason:
                    warnings.append(f"{os.path.basename(folder)} {reason}")
            if len(top_folders) > 10:
                lines.append(f"  ...and {len(top_folders) - 10} more")
            other_total = sum(folder_details[f][1] for f in top_folders)
            if other_total:
                lines.append("")
                lines.append(
                    f"Note: {'this also includes' if len(top_folders) == 1 else 'these also include'} "
                    + ("1 file that isn't a FITS image" if other_total == 1
                       else f"{other_total} files that aren't FITS images")
                    + " (e.g. processed images or project files)."
                )

        if top_files:
            lines.append("")
            lines.append("Files:")
            for path in top_files[:10]:
                lines.append(f"  \u2022 {os.path.basename(path)}")
                try:
                    reason = _may_skip_recycle_bin(path, os.path.getsize(path))
                except OSError:
                    reason = None
                if reason:
                    warnings.append(f"{os.path.basename(path)} {reason}")
            if len(top_files) > 10:
                lines.append(f"  ...and {len(top_files) - 10} more")

        box = QMessageBox(self)
        box.setWindowTitle("Delete marked items")
        box.setText("\n".join(lines))
        if warnings:
            box.setIcon(QMessageBox.Warning)
            box.setInformativeText(
                "WARNING - these may be deleted permanently instead of going to the "
                "Recycle Bin:\n" + "\n".join(f"  \u2022 {w}" for w in warnings)
                + "\n\nWindows skips the Recycle Bin for items that are too big for it, "
                "or on network drives. Back them up first if you might want them back."
            )
        else:
            box.setIcon(QMessageBox.Question)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        self.statusBar().clearMessage()
        if box.exec() != QMessageBox.Yes:
            return

        # ---- delete
        failed = []
        deleted_files = []
        deleted_folders = []
        for path in top_files + top_folders:
            try:
                send2trash(_clean_path(path))
            except Exception as exc:  # noqa: BLE001 - reported via dialog, not raised
                failed.append((path, str(exc)))
                continue
            (deleted_folders if path in top_folders else deleted_files).append(path)

        # ---- tidy up after what was actually deleted
        gone = deleted_files + deleted_folders
        self._close_enlarge_views_inside(gone)
        for path in list(self._marked_for_deletion):
            if any(_is_inside(path, g) for g in gone):
                self._marked_for_deletion.discard(path)
        for path in gone:
            item = self._find_item_for_path(path)
            if item is not None:
                self.grid.takeItem(self.grid.row(item))
            self._info_by_path.pop(path, None)
            self._pixmap_by_path.pop(path, None)

        # Favourites: a favourite folder that was deleted (directly, or
        # because a folder containing it was deleted) is removed. Deleting
        # folders *inside* a favourite leaves the favourite alone.
        removed_favs = [f for f in self._favorites if any(_is_inside(f, d) for d in deleted_folders)]
        if removed_favs:
            self._favorites = [f for f in self._favorites if f not in removed_favs]
            self._save_favorites()
            self._refresh_favorites_list()

        self._update_delete_action()

        # If the folder being viewed was inside something deleted, step up
        # to the nearest folder that still exists.
        if self._current_dir and any(_is_inside(self._current_dir, d) for d in deleted_folders):
            parent = self._current_dir
            while parent and not os.path.isdir(parent):
                up = os.path.dirname(parent)
                if up == parent:
                    break
                parent = up
            if parent and os.path.isdir(parent):
                self._open_folder(parent)

        summary = []
        if deleted_files:
            summary.append(_plural(len(deleted_files), "file"))
        if deleted_folders:
            summary.append(_plural(len(deleted_folders), "folder"))
        done_text = f"Sent {' and '.join(summary) if summary else 'nothing'} to the Recycle Bin"
        if removed_favs:
            done_text += f" (removed {_plural(len(removed_favs), 'favourite')})"

        if failed:
            details = "\n".join(f"{os.path.basename(p)}: {err}" for p, err in failed)
            QMessageBox.warning(
                self, "Some items could not be deleted",
                f"{done_text}.\n{len(failed)} failed:\n{details}",
            )
        else:
            self.statusBar().showMessage(done_text)

    # ---------------------------------------------------------------- thumbnail size
    def _scaled_pixmap(self, pixmap: QPixmap) -> QPixmap:
        """Rescales a decoded thumbnail to the current slider size.

        Thumbnails are decoded once at the slider's maximum size (times the
        screen scaling - see _thumb_decode_size), so this only ever scales
        down. The result is sized in real screen pixels and tagged with the
        screen's scaling ratio, so it stays sharp on high-DPI displays.
        """
        ratio = max(1.0, float(self.devicePixelRatioF()))
        size = self.grid.iconSize()
        target = QSize(int(size.width() * ratio), int(size.height() * ratio))
        scaled = pixmap.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        return scaled

    def _on_thumb_size_changed(self, value: int):
        self.grid.setIconSize(QSize(value, value))
        for i in range(self.grid.count()):
            item = self.grid.item(i)
            path = item.data(Qt.UserRole)
            pixmap = self._pixmap_by_path.get(path)
            if pixmap is not None:
                item.setIcon(QIcon(self._scaled_pixmap(pixmap)))
            item.setSizeHint(QSize(value + 20, value + 40))

    # ---------------------------------------------------------------- enlarge view / marking
    def eventFilter(self, obj, event):
        if obj is self.grid and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Space:
                # Ctrl+Space: a popup for every selected image. (On a Mac,
                # Qt's "Control" is the Cmd key - Cmd+Space is Spotlight -
                # so the real Control key, which Qt calls Meta, works too.)
                if event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
                    self._open_enlarge_views_for_selection()
                else:
                    self._open_enlarge_view()
                return True
            if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                self._toggle_mark_current_item()
                return True
        return super().eventFilter(obj, event)

    MAX_POPUPS_WITHOUT_ASKING = 10
    POPUP_CASCADE_OFFSET = 36  # px between stacked popups

    def _open_enlarge_views_for_selection(self):
        paths = [
            item.data(Qt.UserRole) for item in self._selected_or_current_items()
            if not _is_folder_item(item)
        ]
        if not paths:
            return
        if len(paths) > self.MAX_POPUPS_WITHOUT_ASKING:
            reply = QMessageBox.question(
                self, "Open lots of images?",
                f"Open {len(paths)} images, each in its own window?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        first = None
        for i, path in enumerate(paths):
            dialog = self._open_enlarge_view(path)
            if dialog is None:
                continue
            if first is None:
                first = dialog
            else:
                # Cascade them so each one's title bar stays visible.
                offset = self.POPUP_CASCADE_OFFSET * i
                dialog.move(first.pos().x() + offset, first.pos().y() + offset)

    def _open_enlarge_view(self, path: str | None = None):
        if path is None:
            item = self.grid.currentItem()
            if item is None or _is_folder_item(item):
                return None
            path = item.data(Qt.UserRole)

        dialog = EnlargeDialog(os.path.basename(path), parent=self)
        dialog.setModal(False)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.path = None
        # Each popup's signals carry the popup itself, so actions always
        # apply to the image in the popup where the key was pressed.
        dialog.finished.connect(lambda _r, d=dialog: self._on_enlarge_dialog_closed(d))
        dialog.mark_toggle_requested.connect(lambda d=dialog: self._on_enlarge_mark_toggle_requested(d))
        dialog.navigate_requested.connect(lambda step, d=dialog: self._on_enlarge_navigate(d, step))
        self._enlarge_dialogs.append(dialog)
        dialog.show()
        self._show_in_enlarge_view(dialog, path)
        return dialog

    def _show_in_enlarge_view(self, dialog: EnlargeDialog, path: str):
        """Points a popup at a file and starts decoding it. Used both when
        the popup first opens and when flicking with the arrow keys - the
        previous image stays on screen until the new one is ready, so
        flicking doesn't flash a blank window."""
        if dialog not in self._enlarge_dialogs:
            return
        dialog.path = path
        dialog.show_loading(os.path.basename(path))
        dialog.set_marked(path in self._marked_for_deletion)

        # Connected to a method of this window (not a per-popup lambda) so
        # Qt hands the finished image back on the window's own thread;
        # _on_enlarge_ready then gives it to every popup showing that file.
        worker = ThumbnailWorker(path, thumb_size=ENLARGE_SIZE)
        worker.signals.finished.connect(self._on_enlarge_ready)
        self.thread_pool.start(worker)

    def _on_enlarge_navigate(self, dialog: EnlargeDialog, step: int):
        """Left/right arrow in a popup: move that popup to the previous/next
        image in the grid, keeping the grid selection (and header panel) in
        sync. Stops at the first/last image rather than wrapping around."""
        if dialog not in self._enlarge_dialogs or dialog.path is None:
            return
        item = self._find_item_for_path(dialog.path)
        row = self.grid.row(item) if item is not None else self.grid.currentRow()
        new_row = row + step
        while 0 <= new_row < self.grid.count() and _is_folder_item(self.grid.item(new_row)):
            new_row += step  # folder tiles aren't images - skip past them
        if new_row < 0 or new_row >= self.grid.count():
            self.statusBar().showMessage(
                "First image in folder" if new_row < 0 else "Last image in folder", 2000
            )
            return
        self.grid.setCurrentRow(new_row)
        self._show_in_enlarge_view(dialog, self.grid.item(new_row).data(Qt.UserRole))

    def _on_enlarge_dialog_closed(self, dialog: EnlargeDialog):
        if dialog in self._enlarge_dialogs:
            self._enlarge_dialogs.remove(dialog)

    def _on_enlarge_mark_toggle_requested(self, dialog: EnlargeDialog):
        if dialog not in self._enlarge_dialogs or dialog.path is None:
            return
        path = dialog.path
        self._set_marked(path, path not in self._marked_for_deletion)

    def _refresh_enlarge_marks(self, path: str):
        """Keeps the red MARKED banner right in every popup showing this
        file, however it was marked (grid, right-click, or any popup)."""
        for dialog in self._enlarge_dialogs:
            if dialog.path == path:
                dialog.set_marked(path in self._marked_for_deletion)

    def _close_enlarge_views_inside(self, gone: list[str]):
        """Closes popups whose file was just deleted (or was inside a
        deleted folder)."""
        for dialog in list(self._enlarge_dialogs):
            if dialog.path and any(_is_inside(dialog.path, g) for g in gone):
                dialog.close()

    def _on_enlarge_ready(self, path: str, thumb, info: FitsImageInfo):
        # Goes to every open popup currently showing this file. Popups that
        # were closed, or flicked to a different file before this decode
        # finished, simply don't match.
        targets = [d for d in self._enlarge_dialogs if d.path == path]
        if not targets:
            return
        pixmap = _np_gray_to_pixmap(thumb) if thumb is not None else None
        for dialog in targets:
            if pixmap is None:
                dialog.set_error(info.error or "Could not decode image")
            else:
                dialog.set_pixmap(pixmap)
                dialog.setWindowTitle(os.path.basename(path))

    # ---------------------------------------------------------------- export
    def _export_selected_image(self):
        item = self.grid.currentItem()
        if item is None or _is_folder_item(item):
            return
        path = item.data(Qt.UserRole)

        self._export_path = path
        self.statusBar().showMessage(f"Decoding {os.path.basename(path)} at full resolution...")

        worker = ThumbnailWorker(path, thumb_size=EXPORT_SIZE)
        worker.signals.finished.connect(self._on_export_ready)
        self.thread_pool.start(worker)

    def _on_export_ready(self, path: str, thumb, info: FitsImageInfo):
        # Discards results for a file no longer selected.
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
            export_mode = "RGB" if thumb.ndim == 3 else "L"
            Image.fromarray(thumb, mode=export_mode).save(save_path)
        except Exception as exc:  # noqa: BLE001 - reported via dialog, not raised
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
        if _is_folder_item(current):
            self.filename_label.setText(f"{os.path.basename(path)} (folder - double-click to open)")
            for row in range(self.header_table.rowCount()):
                self.header_table.setItem(row, 1, QTableWidgetItem(""))
            self.export_action.setEnabled(False)
            return
        self.filename_label.setText(os.path.basename(path))
        self.export_action.setEnabled(True)
        info = self._info_by_path.get(path)
        if info is None:
            return

        for row, (key, _label) in enumerate(HEADER_FIELDS):
            value = info.header.get(key, "-")
            self.header_table.setItem(row, 1, QTableWidgetItem(_format_header_value(value)))

    # ---------------------------------------------------------------- update check
    def _start_update_check(self):
        worker = UpdateCheckWorker(APP_VERSION)
        worker.signals.finished.connect(self._on_update_check_finished)
        self.thread_pool.start(worker)

    def _on_update_check_finished(self, latest_tag: str, release_url: str):
        if not latest_tag:
            return  # up to date, or the check failed - either way, no notice

        box = QMessageBox(self)
        box.setWindowTitle("Update available")
        box.setText(
            f"A newer version of FITS Viewer is available: {latest_tag}\n"
            f"(you have v{APP_VERSION})"
        )
        download_button = box.addButton("Download", QMessageBox.AcceptRole)
        box.addButton("Later", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() == download_button and release_url:
            QDesktopServices.openUrl(QUrl(release_url))
