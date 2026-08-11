"""
Disk cache for generated thumbnails, so reopening a folder of a few hundred
FITS files doesn't mean re-decoding every one of them every time.

Cache key is derived from the file's absolute path + file size + mtime +
requested thumbnail size, so a changed or replaced file is regenerated
automatically without needing any manual "clear cache" step, and asking
for the same file at a different resolution (e.g. grid thumbnail vs.
full-size enlarge view) doesn't collide with a smaller cached entry.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PIL import Image
import numpy as np


def _default_cache_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".cache"
    cache_dir = base / "FitsBridge" / "thumbnails"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


class ThumbnailCache:
    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or _default_cache_dir()

    def _key_for(self, path: str, size: int) -> str:
        st = os.stat(path)
        raw = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{size}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _cache_path(self, path: str, size: int) -> Path:
        return self.cache_dir / f"{self._key_for(path, size)}.png"

    def get(self, path: str, size: int) -> np.ndarray | None:
        try:
            cache_file = self._cache_path(path, size)
        except OSError:
            return None
        if not cache_file.exists():
            return None
        try:
            with Image.open(cache_file) as im:
                return np.array(im.convert("L"))
        except Exception:  # noqa: BLE001 - corrupt cache entry, just regenerate
            return None

    def put(self, path: str, thumb: np.ndarray, size: int) -> None:
        try:
            cache_file = self._cache_path(path, size)
        except OSError:
            return
        try:
            Image.fromarray(thumb, mode="L").save(cache_file, format="PNG")
        except Exception:  # noqa: BLE001 - caching is a nice-to-have, never fatal
            pass
