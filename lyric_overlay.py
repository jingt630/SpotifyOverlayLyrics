"""Always-on-top lyric pill with a light glass rim.

Run this file to see the overlay. The body is a thin white veil so the
desktop stays visible. The bright edge is the glass. Drag the pill to move
it. Minimize and close sit at the top right.

The two lines are the singing line and the next line. Both use one font.
connect_spotify.py fills them from playback. Running this file alone shows
a sample pair.
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QWidget

# White veil. High enough to stay a light glass, low enough that the window
# behind still tints it. The reference image is much closer to solid white.
_FILL_ALPHA = 168
_SHEEN_ALPHA = 55
_BUTTON = 22.0
_BUTTON_GAP = 6.0
# Same inset on both sides, so line 1 starts on the left edge of the glass
# and line 2 ends on the right edge.
_TEXT_PAD = 36


def _pill_path(rect: QRectF) -> QPainterPath:
    """Return a capsule path for rect."""
    path = QPainterPath()
    radius = rect.height() / 2
    path.addRoundedRect(rect, radius, radius)
    return path


class LyricOverlay(QWidget):
    """Frameless glass pill that shows the singing line and the next line.

    The window stays above other windows. The fill is a thin white veil.
    A luminous rim marks the glass edge.
    """

    def __init__(self, singing: str, upcoming: str) -> None:
        """Build the pill around two lyric lines.

        Purpose:
            Size the window to the text and prepare it as a transparent,
            always-on-top overlay.

        Parameters:
            singing (str): The line being sung. Drawn on the left.
            upcoming (str): The next line. Its end sits on the right edge of
                the glass. Empty hides that line.

        Returns:
            None.

        Precondition:
            A QApplication already exists. singing is non-empty.

        Postcondition:
            The widget is not shown yet. Dragging and Escape are armed.
        """
        super().__init__()
        self.singing = singing
        self.upcoming = upcoming
        self._drag_offset: QPoint | None = None
        self._hot: str | None = None
        self.setWindowTitle("Spotify Lyric Overlay")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self._fit_to_text()

    def set_lines(self, singing: str, upcoming: str) -> None:
        """Replace the two lines and resize the pill around them.

        Purpose:
            Let a later playback loop update the overlay without rebuilding it.

        Parameters:
            singing (str): The line being sung.
            upcoming (str): The next line. Empty draws the singing line alone.

        Returns:
            None.

        Precondition:
            The widget has been constructed.

        Postcondition:
            The window size matches the new text. The next paint shows it.
        """
        self.singing = singing
        self.upcoming = upcoming
        self._fit_to_text()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Draw the veil, the rim, and the two lyric lines."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        outer = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        path = _pill_path(outer)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        for width, alpha in ((18, 28), (9, 46), (3, 72)):
            painter.setPen(QPen(QColor(214, 230, 255, alpha), width))
            painter.drawPath(path)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, _FILL_ALPHA))
        painter.drawPath(path)

        painter.setClipPath(path)
        sheen = QRectF(outer.x(), outer.y(), outer.width(), outer.height() * 0.22)
        painter.fillRect(sheen, QColor(255, 255, 255, _SHEEN_ALPHA))
        painter.setClipping(False)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 150), 1.15))
        painter.drawPath(path)

        self._draw_controls(painter, outer)
        self._draw_lines(painter, self._lyric_bounds(outer))
        painter.end()

    def keyPressEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Close the overlay when Escape is pressed."""
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Minimize, close, or start a drag from the pressed point."""
        if event.button() == Qt.MouseButton.LeftButton:
            control = self._control_at(event.position())
            if control == "close":
                self.close()
                return
            if control == "minimize":
                self.showMinimized()
                return
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Move the pill, or highlight the control under the pointer."""
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            return
        hot = self._control_at(event.position())
        if hot != self._hot:
            self._hot = hot
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Clear the control highlight when the pointer leaves the window."""
        self._hot = None
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Stop dragging when the left button is released."""
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def _fit_to_text(self) -> None:
        """Size the window so both lines sit inside the pill with room for the rim."""
        metrics = QFontMetrics(_lyric_font())
        singing_width = metrics.horizontalAdvance(self.singing)
        upcoming_width = metrics.horizontalAdvance(self.upcoming) if self.upcoming else 0
        text_width = max(singing_width, upcoming_width, 1)
        text_height = metrics.height()
        if self.upcoming:
            text_height += 12 + metrics.height()
        pad_y = 28
        controls = 12 + _BUTTON + 8
        rim = 16
        width = int(text_width + _TEXT_PAD * 2 + rim)
        height = int(text_height + pad_y * 2 + controls + rim)
        self.resize(width, height)

    def _control_rects(self, outer: QRectF) -> tuple[QRectF, QRectF]:
        """Return the minimize rect and the close rect, from left to right."""
        close = QRectF(
            outer.right() - 16 - _BUTTON,
            outer.top() + 12,
            _BUTTON,
            _BUTTON,
        )
        minimize = QRectF(close.left() - _BUTTON_GAP - _BUTTON, close.top(), _BUTTON, _BUTTON)
        return minimize, close

    def _control_at(self, pos: QPointF) -> str | None:
        """Return which top-right control contains pos, if either does."""
        outer = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        minimize, close = self._control_rects(outer)
        if close.contains(pos):
            return "close"
        if minimize.contains(pos):
            return "minimize"
        return None

    def _lyric_bounds(self, outer: QRectF) -> QRectF:
        """Return the area under the controls where the two lyric lines sit."""
        top = outer.top() + 12 + _BUTTON + 6
        return QRectF(outer.x(), top, outer.width(), outer.bottom() - top)

    def _draw_controls(self, painter: QPainter, outer: QRectF) -> None:
        """Draw minimize and close at the top right of the pill."""
        minimize, close = self._control_rects(outer)
        self._draw_control(painter, minimize, "minimize")
        self._draw_control(painter, close, "close")

    def _draw_control(self, painter: QPainter, rect: QRectF, kind: str) -> None:
        """Draw one round window control and its icon."""
        hovered = self._hot == kind
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 150 if hovered else 70))
        painter.drawEllipse(rect)
        painter.setPen(QPen(QColor(20, 20, 20), 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if kind == "minimize":
            y = rect.center().y()
            painter.drawLine(QPointF(rect.left() + 6, y), QPointF(rect.right() - 6, y))
            return
        inset = 6.5
        painter.drawLine(QPointF(rect.left() + inset, rect.top() + inset), QPointF(rect.right() - inset, rect.bottom() - inset))
        painter.drawLine(QPointF(rect.right() - inset, rect.top() + inset), QPointF(rect.left() + inset, rect.bottom() - inset))

    def _draw_lines(self, painter: QPainter, outer: QRectF) -> None:
        """Draw both lyric lines in one font, flush to opposite edges of the glass.

        The singing line starts at the left edge of the text area. The next
        line ends at the right edge of that same area.
        """
        font = _lyric_font()
        metrics = QFontMetrics(font)
        content = outer.adjusted(_TEXT_PAD, 0, -_TEXT_PAD, 0)
        line_height = metrics.height()
        block_height = line_height + (12 + line_height if self.upcoming else 0)
        top = content.y() + max(0, (content.height() - block_height) / 2)
        painter.setPen(QColor(12, 12, 12))
        painter.setFont(font)
        painter.drawText(
            QRectF(content.x(), top, content.width(), line_height),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.singing,
        )
        if not self.upcoming:
            return
        painter.drawText(
            QRectF(content.x(), top + line_height + 12, content.width(), line_height),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            self.upcoming,
        )


def _lyric_font() -> QFont:
    """Return the one font used for both lyric lines."""
    font = QFont("Segoe UI", 18)
    font.setWeight(QFont.Weight.Normal)
    return font


def main() -> None:
    """Open the glass lyric pill on the primary screen.

    Purpose:
        Show the overlay by itself so the glass can be judged before it is
        tied to playback.

    Parameters:
        None.

    Returns:
        None.

    Precondition:
        A desktop session is available.

    Postcondition:
        The function returns when the window closes.
    """
    app = QApplication([])
    window = LyricOverlay(
        "The city sleeps beneath the rain",
        "but my heart still calls your name",
    )
    frame = window.frameGeometry()
    screen = app.primaryScreen().availableGeometry().center()
    frame.moveCenter(screen)
    window.move(frame.topLeft())
    window.show()
    window.raise_()
    window.activateWindow()
    print("Lyric overlay is open. Drag the glass to move it. Minimize and close are at the top right.")
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
