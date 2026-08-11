"""
QRunnable-based background worker: decodes + stretches a FITS file into a
thumbnail off the UI thread, checking the disk cache first.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .cache import ThumbnailCache
from .fits_utils import make_thumbnail_array, read_fits_info, FitsImageInfo

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
