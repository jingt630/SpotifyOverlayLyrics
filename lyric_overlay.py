"""Always-on-top lyric pill with a light glass rim.

Run this file to see the overlay. The body is a thin white veil so the
desktop stays visible. The bright edge is the glass. Drag the pill to move
it. Minimize and close sit at the top right.

The two lines are the singing line and the next line. Both use one font.
connect_spotify.py fills them from playback. Running this file alone shows
a sample pair.
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QWidget

from lrclib_client import LyricCandidate

# White veil. High enough to stay a light glass, low enough that the window
# behind still tints it. The reference image is much closer to solid white.
_FILL_ALPHA = 168
_SHEEN_ALPHA = 55
_BUTTON = 22.0
_BUTTON_GAP = 6.0
# Same inset on both sides, so line 1 starts on the left edge of the glass
# and line 2 ends on the right edge.
_TEXT_PAD = 36


def _pill_path(rect: QRectF, radius: float | None = None) -> QPainterPath:
    """Return a rounded path for rect. The default radius is a capsule."""
    path = QPainterPath()
    curve = rect.height() / 2 if radius is None else radius
    path.addRoundedRect(rect, curve, curve)
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
        self._mode = "lyrics"
        self._choice_rows: list[LyricCandidate] = []
        self._on_pick = None
        self._hits: list[tuple[QRectF, str, str | int]] = []
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
        self._mode = "lyrics"
        self._choice_rows = []
        self._on_pick = None
        self._hits = []
        self.singing = singing
        self.upcoming = upcoming
        self._fit_to_text()
        self.update()

    def set_choices(self, candidates: list[LyricCandidate], on_pick) -> None:
        """Show the candidate links and a Use this button for each one.

        Purpose:
            Let a shortcut user preview a match in the browser and choose it
            without a terminal. Lyric lines themselves stay plain text.

        Parameters:
            candidates (list[LyricCandidate]): Up to five synced records.
            on_pick: Called with the LRCLIB id when Use this is clicked.

        Returns:
            None.

        Precondition:
            candidates is non-empty.

        Postcondition:
            The window is tall enough for every candidate. Links are clickable.
            Use this selects that record.
        """
        self._mode = "choices"
        self._choice_rows = list(candidates)
        self._on_pick = on_pick
        self.singing = ""
        self.upcoming = ""
        self._fit_to_choices()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Draw the veil, the rim, and the two lyric lines."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        outer = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        radius = outer.height() / 2 if self._mode == "lyrics" else min(outer.height() / 2, 28)
        path = _pill_path(outer, radius)

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
        self._hits = []
        bounds = self._lyric_bounds(outer)
        if self._mode == "choices":
            self._draw_choices(painter, bounds)
        else:
            self._draw_lines(painter, bounds)
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
            hit = self._hit_at(event.position())
            if hit is not None and hit[0] == "link":
                QDesktopServices.openUrl(QUrl(str(hit[1])))
                return
            if hit is not None and hit[0] == "use":
                if self._on_pick is not None:
                    self._on_pick(int(hit[1]))
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
        hit = self._hit_at(event.position())
        if hit is not None:
            hot = f"{hit[0]}:{hit[1]}"
        if hot != self._hot:
            self._hot = hot
            self.update()
        if hit is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Clear the control highlight when the pointer leaves the window."""
        self._hot = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
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

    def _fit_to_choices(self) -> None:
        """Size the window to the candidate list, one block per LRCLIB record."""
        text_width, text_height = self._choice_size()
        controls = 12 + _BUTTON + 8
        rim = 16
        pad_y = 20
        self.resize(int(text_width + _TEXT_PAD * 2 + rim), int(text_height + pad_y * 2 + controls + rim))

    def _choice_size(self) -> tuple[float, float]:
        """Return the text width and height of the candidate list."""
        metrics = QFontMetrics(_lyric_font())
        line_h = metrics.height()
        button_w = metrics.horizontalAdvance("Use this") + 22
        header = metrics.horizontalAdvance("Pick a synced lyric")
        widths = [header]
        for row in self._choice_rows:
            widths.append(min(metrics.horizontalAdvance(row.label), 560))
            widths.append(metrics.horizontalAdvance(row.url) + 12 + button_w)
        text_width = max(widths) if widths else 240
        text_height = line_h + 10 + len(self._choice_rows) * (line_h * 2 + 18)
        return text_width, text_height

    def _hit_at(self, pos: QPointF) -> tuple[str, str | int] | None:
        """Return the link or Use this control under pos."""
        for rect, kind, payload in self._hits:
            if rect.contains(pos):
                return kind, payload
        return None

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

    def _draw_choices(self, painter: QPainter, bounds: QRectF) -> None:
        """Draw each candidate left-aligned, with a preview link and a Use this button."""
        font = _lyric_font()
        metrics = QFontMetrics(font)
        line_h = metrics.height()
        button_w = metrics.horizontalAdvance("Use this") + 22
        button_h = line_h + 6
        content = bounds.adjusted(_TEXT_PAD, 8, -_TEXT_PAD, -8)
        painter.setFont(font)
        y = content.y()
        painter.setPen(QColor(12, 12, 12))
        painter.drawText(QRectF(content.x(), y, content.width(), line_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Pick a synced lyric")
        y += line_h + 10
        for row in self._choice_rows:
            label = metrics.elidedText(row.label, Qt.TextElideMode.ElideRight, int(content.width()))
            painter.setPen(QColor(12, 12, 12))
            painter.drawText(QRectF(content.x(), y, content.width(), line_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            y += line_h + 4
            link_w = metrics.horizontalAdvance(row.url)
            link_rect = QRectF(content.x(), y, link_w, line_h)
            link_hot = self._hot == f"link:{row.url}"
            painter.setPen(QColor(20, 70, 160))
            painter.drawText(link_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, row.url)
            if link_hot:
                painter.drawLine(QPointF(link_rect.left(), link_rect.bottom() - 1), QPointF(link_rect.right(), link_rect.bottom() - 1))
            self._hits.append((link_rect, "link", row.url))
            button = QRectF(link_rect.right() + 12, y - 3, button_w, button_h)
            use_hot = self._hot == f"use:{row.record_id}"
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 180 if use_hot else 90))
            painter.drawRoundedRect(button, 8, 8)
            painter.setPen(QColor(12, 12, 12))
            painter.drawText(button, Qt.AlignmentFlag.AlignCenter, "Use this")
            self._hits.append((button, "use", row.record_id))
            y += line_h + 14


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
