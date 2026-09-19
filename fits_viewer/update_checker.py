"""
Startup check for a newer GitHub release than the currently running
version. Runs once, off the UI thread (same QRunnable pattern as
ThumbnailWorker), and stays silent on any failure - offline, GitHub
unreachable, or a malformed response should never interrupt normal use
of the app with an error dialog.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

GITHUB_RELEASES_API_URL = "https://api.github.com/repos/Nicole-Jenkins/FITS-Viewer/releases/latest"
REQUEST_TIMEOUT_SECONDS = 5


def _parse_version(tag: str) -> tuple[int, ...] | None:
    """Parses a tag such as 'v1.2.0' into (1, 2, 0). Returns None for any
    tag not in this dotted-numeric format."""
    cleaned = tag.lstrip("vV")
    parts = cleaned.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def is_newer(latest_tag: str, current_version: str) -> bool:
    """True only if latest_tag parses to a strictly greater version than
    current_version. Malformed input on either side returns False rather
    than raising."""
    latest = _parse_version(latest_tag)
    current = _parse_version(current_version)
    if latest is None or current is None:
        return False
    return latest > current


class UpdateCheckSignals(QObject):
    # (latest_tag, release_url) - both empty strings when no update is
    # available or the check failed for any reason.
    finished = Signal(str, str)


class UpdateCheckWorker(QRunnable):
    def __init__(self, current_version: str):
        super().__init__()
        self.current_version = current_version
        self.signals = UpdateCheckSignals()

    @Slot()
    def run(self) -> None:
        try:
            request = urllib.request.Request(
                GITHUB_RELEASES_API_URL,
                headers={"Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                data = json.loads(response.read().decode("utf-8"))
            latest_tag = data.get("tag_name", "")
            release_url = data.get("html_url", "")
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
            self.signals.finished.emit("", "")
            return

        if latest_tag and is_newer(latest_tag, self.current_version):
            self.signals.finished.emit(latest_tag, release_url)
        else:
            self.signals.finished.emit("", "")
