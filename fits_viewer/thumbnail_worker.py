"""
QRunnable-based background workers, so the window never freezes:
  - ThumbnailWorker decodes + stretches a FITS file into a thumbnail,
    checking the disk cache first.
  - FolderPreviewWorker looks inside a subfolder (counts its FITS files and
    subfolders, and makes a thumbnail of the first image it finds) for the
    folder tiles in the grid.
"""

from __future__ import annotations

import os
from collections import deque

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .cache import ThumbnailCache
from .fits_utils import make_thumbnail_array, read_fits_info, FitsImageInfo, is_fits_filename

_cache = ThumbnailCache()


class ThumbnailSignals(QObject):
    finished = Signal(str, object, object)  # path, thumbnail(np.ndarray | None), info(FitsImageInfo)


class ThumbnailWorker(QRunnable):
    def __init__(self, path: str, thumb_size: int = 220):
        super().__init__()
        self.path = path
        self.thumb_size = thumb_size
        self.signals = ThumbnailSignals()

    @Slot()
    def run(self) -> None:
        info: FitsImageInfo = read_fits_info(self.path)

        thumb = _cache.get(self.path, self.thumb_size)
        if thumb is None:
            try:
                thumb = make_thumbnail_array(self.path, max_size=self.thumb_size)
                _cache.put(self.path, thumb, self.thumb_size)
            except Exception as exc:  # noqa: BLE001 - surfaced via info.error, not a crash
                thumb = None
                if info.error is None:
                    info.error = str(exc)

        self.signals.finished.emit(self.path, thumb, info)


# How far down to look for a preview image (1 = only the folder's direct
# subfolders, 2 = their subfolders too), and a cap on how many folder
# entries to look at, so a huge folder tree can't stall the preview.
PREVIEW_SEARCH_DEPTH = 2
PREVIEW_MAX_ENTRIES = 5000


def is_hidden_or_system_folder(name: str) -> bool:
    """Folders that should never show as tiles: hidden ones (.git etc) and
    Windows system folders found at the top of drives."""
    return name.startswith((".", "$")) or name.lower() in {
        "system volume information", "__pycache__",
    }


def scan_folder(folder: str):
    """Returns (fits_files_directly_inside, subfolders_directly_inside,
    first_fits_file_found_or_None). The preview file is the first FITS file
    (alphabetically) directly inside the folder; if there isn't one, the
    search moves down into subfolders, up to PREVIEW_SEARCH_DEPTH levels."""
    fits_count = 0
    sub_count = 0
    first_file = None
    examined = 0
    queue = deque([(folder, 0)])
    while queue:
        current, depth = queue.popleft()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            continue
        subdirs = []
        for entry in entries:
            examined += 1
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if not is_hidden_or_system_folder(entry.name):
                    subdirs.append(entry.path)
            elif is_fits_filename(entry.name):
                if depth == 0:
                    fits_count += 1
                if first_file is None:
                    first_file = entry.path
        if depth == 0:
            sub_count = len(subdirs)
        if first_file is None and depth < PREVIEW_SEARCH_DEPTH and examined < PREVIEW_MAX_ENTRIES:
            queue.extend((sub, depth + 1) for sub in subdirs)
        elif depth > 0 and first_file is not None:
            break
    return fits_count, sub_count, first_file


class FolderPreviewSignals(QObject):
    finished = Signal(str, int, int, object)  # folder, fits_count, subfolder_count, thumbnail(np.ndarray | None)


class FolderPreviewWorker(QRunnable):
    def __init__(self, folder: str, thumb_size: int):
        super().__init__()
        self.folder = folder
        self.thumb_size = thumb_size
        self.signals = FolderPreviewSignals()

    @Slot()
    def run(self) -> None:
        fits_count, sub_count, first_file = scan_folder(self.folder)
        thumb = None
        if first_file is not None:
            thumb = _cache.get(first_file, self.thumb_size)
            if thumb is None:
                try:
                    thumb = make_thumbnail_array(first_file, max_size=self.thumb_size)
                    _cache.put(first_file, thumb, self.thumb_size)
                except Exception:  # noqa: BLE001 - no preview is fine, tile still shows
                    thumb = None
        self.signals.finished.emit(self.folder, fits_count, sub_count, thumb)
