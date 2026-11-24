"""
Full-size image viewer: the "hit space to pop up bigger, then zoom/pan"
view, modelled on Adobe Bridge's preview behaviour.

Kept in its own file for the same reason the rest of the app is split up
(see HOW_IT_WORKS.md) - this is GUI-only concern, separate from decoding
and separate from the grid/tree/header wiring in main_window.py.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QPixmap, QWheelEvent, QTransform, QPainter
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QGraphicsView, QGraphicsScene,
    QGraphicsPixmapItem,
)

MIN_ZOOM = 0.1
MAX_ZOOM = 20.0
ZOOM_IN_FACTOR = 1.25
ZOOM_OUT_FACTOR = 0.8


class EnlargeDialog(QDialog):
    """Non-modal popup showing one image full-size, zoomable with the
    mouse wheel and pannable by dragging (QGraphicsView's built-in
    ScrollHandDrag). Space or Escape closes it, matching the key that
    opened it. Delete/Backspace emits mark_toggle_requested instead of
    closing - marking state is owned by MainWindow, this dialog only
    reflects it via set_marked(). Left/Right arrows emit
    navigate_requested(-1/+1) so MainWindow can load the previous/next
    image into this same window."""

    mark_toggle_requested = Signal()
    navigate_requested = Signal(int)

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 800)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.mark_banner = QLabel("MARKED FOR DELETION")
        self.mark_banner.setAlignment(Qt.AlignCenter)
        self.mark_banner.setStyleSheet(
            "background-color: #782020; color: white; font-weight: 600; padding: 6px;"
        )
        self.mark_banner.hide()
        layout.addWidget(self.mark_banner)

        self.status_label = QLabel("Loading full-size image...")
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.view.setRenderHint(QPainter.SmoothPixmapTransform)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setBackgroundBrush(Qt.black)
        self.view.hide()
        # QGraphicsView normally uses the arrow keys to scroll, so they never
        # reach this dialog's keyPressEvent - that's why left/right did
        # nothing before. Intercept them on the view (and its viewport,
        # which is what actually has focus after clicking the image).
        self.view.installEventFilter(self)
        self.view.viewport().installEventFilter(self)
        layout.addWidget(self.view)

        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._base_transform = QTransform()
        self._zoom = 1.0

    def set_marked(self, marked: bool) -> None:
        self.mark_banner.setVisible(marked)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self.status_label.hide()
        self.view.show()
        self.scene.clear()
        self._pixmap_item = self.scene.addPixmap(pixmap)
        self.scene.setSceneRect(self._pixmap_item.boundingRect())
        self.view.fitInView(self._pixmap_item, Qt.KeepAspectRatio)
        # Remember the "fit to window" transform as the zoom=1.0 baseline,
        # so wheel-zoom scales relative to a sensible starting point
        # instead of the image's raw pixel size.
        self._base_transform = self.view.transform()
        self._zoom = 1.0

    def show_loading(self, name: str) -> None:
        """Called when switching to a new file. Keeps the previous image on
        screen until the new one arrives; only the title changes."""
        self.setWindowTitle(f"{name} - loading...")
        if self._pixmap_item is None:
            self.status_label.setText("Loading full-size image...")
            self.status_label.show()

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Left, Qt.Key_Right):
            self.navigate_requested.emit(-1 if event.key() == Qt.Key_Left else 1)
            return True
        return super().eventFilter(obj, event)

    def set_error(self, message: str) -> None:
        self.view.hide()
        self.status_label.setText(message)
        self.status_label.show()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._pixmap_item is None:
            super().wheelEvent(event)
            return
        factor = ZOOM_IN_FACTOR if event.angleDelta().y() > 0 else ZOOM_OUT_FACTOR
        self._zoom = max(MIN_ZOOM, min(self._zoom * factor, MAX_ZOOM))
        self.view.setTransform(self._base_transform)
        self.view.scale(self._zoom, self._zoom)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Escape, Qt.Key_Space):
            self.close()
        elif event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.mark_toggle_requested.emit()
        elif event.key() in (Qt.Key_Left, Qt.Key_Right):
            self.navigate_requested.emit(-1 if event.key() == Qt.Key_Left else 1)
        else:
            super().keyPressEvent(event)
