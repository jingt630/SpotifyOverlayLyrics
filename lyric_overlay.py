"""Transparent lyric overlay.

The window is clear until the pointer is over it. Hover shows a light black
tint and a centered control bar. Lyrics stay visible either way.

Two-line mode puts one line on the left and one on the right. The line being
sung is darker. One-line mode centers that line. The window sizes itself to
the text, between a minimum and a maximum taken from the monitor.
"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter
from PyQt6.QtWidgets import QApplication, QWidget

from lrclib_client import LyricCandidate

_SETTINGS_PATH = Path(__file__).resolve().parent / ".spotify" / "overlay_ui.json"
_FONT_MIN = 16
_FONT_MAX = 48
_FONT_DEFAULT = 26
_HOVER_ALPHA = 26
_PAD_X = 28
_PAD_Y = 18
_BAR_H = 40
_ICON = 28
_ICON_GAP = 10

_TEXT = {
    "en": {
        "language": "Language",
        "language_value": "English",
        "lines": "Lines",
        "two": "Two lines",
        "one": "One line",
        "font": "Font size",
        "pick": "Pick a synced lyric",
        "use": "Use this",
        "idle": "Nothing is currently playing.",
        "none": "No synced lyrics for this track.",
        "busy": "LRCLIB is busy. Retrying…",
        "loading": "Loading lyrics…",
        "need_scope": "Sign in again to use the playback buttons.",
        "need_device": "No active Spotify device.",
        "playback_failed": "Spotify did not change playback.",
    },
    "zh": {
        "language": "语言",
        "language_value": "中文",
        "lines": "行数",
        "two": "两行",
        "one": "一行",
        "font": "字号",
        "pick": "选择同步歌词",
        "use": "使用",
        "idle": "当前没有在播放。",
        "none": "这首歌没有同步歌词。",
        "busy": "歌词库繁忙，正在重试…",
        "loading": "正在加载歌词…",
        "need_scope": "请重新登录后再使用播放按钮。",
        "need_device": "没有正在使用的 Spotify 设备。",
        "playback_failed": "Spotify 没有切换播放。",
    },
}

# Segoe Fluent Icons / MDL2 code points. The bar is drawn with these glyphs.
_GLYPH = {
    "settings": "\uE713",
    "lock": "\uE72E",
    "unlock": "\uE785",
    "previous": "\uE892",
    "pause": "\uE769",
    "play": "\uE768",
    "next": "\uE893",
    "minimize": "\uE921",
    "close": "\uE8BB",
}


def _load_ui() -> dict:
    """Return the saved language, line mode, and font size."""
    saved = {"language": "en", "lines": "two", "font": _FONT_DEFAULT}
    if not _SETTINGS_PATH.exists():
        return saved
    try:
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return saved
    if raw.get("language") in _TEXT:
        saved["language"] = raw["language"]
    if raw.get("lines") in ("one", "two"):
        saved["lines"] = raw["lines"]
    font = raw.get("font")
    if isinstance(font, int):
        saved["font"] = max(_FONT_MIN, min(_FONT_MAX, font))
    return saved


def _save_ui(language: str, lines: str, font: int) -> None:
    """Write the overlay settings next to the other local Spotify files."""
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(
        json.dumps({"language": language, "lines": lines, "font": font}, indent=2),
        encoding="utf-8",
    )


def _icon_font(size: int) -> QFont:
    """Return a Windows icon font at size points."""
    font = QFont("Segoe Fluent Icons", size)
    if not QFontMetrics(font).inFontUcs4(0xE713):
        font = QFont("Segoe MDL2 Assets", size)
    return font


def _ui_font(size: int) -> QFont:
    """Return the font used for settings labels."""
    font = QFont("Segoe UI", size)
    font.setWeight(QFont.Weight.Normal)
    return font


class LyricOverlay(QWidget):
    """Frameless lyric window. Hover reveals the bar and a faint black fill."""

    def __init__(self, left: str = "", right: str = "", highlight: str = "left") -> None:
        """Build a clear overlay sized to the lyric text.

        Purpose:
            Prepare the window, load saved display settings, and arm dragging.

        Parameters:
            left (str): Text for the left slot in two-line mode.
            right (str): Text for the right slot. Empty hides that slot.
            highlight (str): "left", "right", or "single". "single" centers one line.

        Returns:
            None.

        Precondition:
            A QApplication already exists.

        Postcondition:
            The widget is not shown yet. It stays above other windows.
        """
        super().__init__()
        ui = _load_ui()
        self._language = ui["language"]
        self._line_mode = ui["lines"]
        self._font_size = ui["font"]
        self.left = left
        self.right = right
        self._highlight = highlight
        self._status_key = ""
        self._notice_key = ""
        self._mode = "lyrics"
        self._choice_rows: list[LyricCandidate] = []
        self._on_pick = None
        self._on_previous = None
        self._on_toggle = None
        self._on_next = None
        self._playing = True
        self._on_top = True
        self._chrome = False
        self._settings_open = False
        self._hits: list[tuple[QRectF, str, str | int]] = []
        self._drag_offset: QPoint | None = None
        self._hot: str | None = None
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._clear_notice)
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
        self.setMouseTracking(True)
        self._apply_size()

    def text(self, key: str) -> str:
        """Return one interface string in the selected language."""
        return _TEXT[self._language].get(key, _TEXT["en"].get(key, key))

    def set_transport(self, on_previous, on_toggle, on_next) -> None:
        """Store the callbacks for previous, pause or resume, and next."""
        self._on_previous = on_previous
        self._on_toggle = on_toggle
        self._on_next = on_next

    def set_playing(self, playing: bool) -> None:
        """Switch the middle transport icon between pause and play."""
        if playing == self._playing:
            return
        self._playing = playing
        self.update()

    def set_notice(self, key: str) -> None:
        """Show a short playback message, then clear it."""
        self._notice_key = key
        self._notice_timer.start(4000)
        self._relayout("top")

    def set_status(self, key: str) -> None:
        """Replace the lyrics with one translated status line."""
        self._mode = "status"
        self._status_key = key
        self._choice_rows = []
        self._on_pick = None
        self.left = ""
        self.right = ""
        self._relayout("top")

    def set_lines(self, left: str, right: str, highlight: str = "left") -> None:
        """Show a lyric pair. highlight is the side being sung.

        Purpose:
            Update the words without rebuilding the window. One-line mode
            uses only the highlighted side and centers it.

        Parameters:
            left (str): Left slot.
            right (str): Right slot.
            highlight (str): "left", "right", or "single".

        Returns:
            None.

        Precondition:
            The widget has been constructed.

        Postcondition:
            The window size matches the new text. Candidate links are hidden.
        """
        self._mode = "lyrics"
        self._status_key = ""
        self._choice_rows = []
        self._on_pick = None
        self.left = left
        self.right = right
        self._highlight = highlight
        self._relayout("top")

    def set_choices(self, candidates: list[LyricCandidate], on_pick) -> None:
        """Show preview links and a Use this button for each candidate.

        Purpose:
            Let a shortcut user preview a match in the browser and choose it.
            Lyric lines themselves stay plain text.

        Parameters:
            candidates (list[LyricCandidate]): Up to five synced records.
            on_pick: Called with the LRCLIB id when Use this is clicked.

        Returns:
            None.

        Precondition:
            candidates is non-empty.

        Postcondition:
            The window is tall enough for every candidate. Links open in the browser.
        """
        self._mode = "choices"
        self._choice_rows = list(candidates)
        self._on_pick = on_pick
        self.left = ""
        self.right = ""
        self._status_key = ""
        self._relayout("top")

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Draw the hover fill, the bar, and the lyric text."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self._hits = []
        if self._chrome or self._settings_open:
            painter.fillRect(self.rect(), QColor(0, 0, 0, _HOVER_ALPHA))
        if self._chrome or self._settings_open:
            self._draw_bar(painter)
        body = self._body_rect()
        if self._mode == "choices":
            self._draw_choices(painter, body)
        elif self._mode == "status":
            self._draw_status(painter, body)
        else:
            self._draw_lyrics(painter, body)
        if self._settings_open:
            self._draw_settings(painter)
        if self._notice_key:
            self._draw_notice(painter)
        painter.end()

    def enterEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Reveal the bar and the black tint."""
        self._set_chrome(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Hide the bar unless settings are open."""
        self._hot = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if not self._settings_open:
            self._set_chrome(False)
        else:
            self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Close the overlay when Escape is pressed."""
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Run the control under the pointer, or start a drag."""
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        hit = self._hit_at(event.position())
        if hit is not None:
            self._activate(hit)
            return
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Move the window, or point at a control."""
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            return
        hit = self._hit_at(event.position())
        hot = None if hit is None else f"{hit[0]}:{hit[1]}"
        if hot != self._hot:
            self._hot = hot
            self.update()
        if hit is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Stop dragging."""
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def _activate(self, hit: tuple[str, str | int]) -> None:
        """Carry out one bar, settings, or candidate click."""
        kind, payload = hit
        if kind == "settings":
            self._settings_open = not self._settings_open
            self._set_chrome(True)
            self._relayout("top")
            return
        if kind == "lock":
            self._on_top = not self._on_top
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self._on_top)
            self.show()
            return
        if kind == "previous" and self._on_previous is not None:
            self._on_previous()
            return
        if kind == "pause" and self._on_toggle is not None:
            self._on_toggle()
            return
        if kind == "next" and self._on_next is not None:
            self._on_next()
            return
        if kind == "minimize":
            self.showMinimized()
            return
        if kind == "close":
            self.close()
            return
        if kind == "lang":
            self._language = "zh" if self._language == "en" else "en"
            self._persist()
            self._relayout("top")
            return
        if kind == "lines":
            self._line_mode = "one" if self._line_mode == "two" else "two"
            self._persist()
            self._relayout("top")
            return
        if kind == "font-down":
            self._font_size = max(_FONT_MIN, self._font_size - 2)
            self._persist()
            self._relayout("top")
            return
        if kind == "font-up":
            self._font_size = min(_FONT_MAX, self._font_size + 2)
            self._persist()
            self._relayout("top")
            return
        if kind == "link":
            QDesktopServices.openUrl(QUrl(str(payload)))
            return
        if kind == "use" and self._on_pick is not None:
            self._on_pick(int(payload))

    def _persist(self) -> None:
        """Save language, line mode, and font size."""
        _save_ui(self._language, self._line_mode, self._font_size)

    def _clear_notice(self) -> None:
        """Remove the playback message."""
        self._notice_key = ""
        self._relayout("top")

    def _set_chrome(self, visible: bool) -> None:
        """Show or hide the bar, keeping the lyric block in the same screen place."""
        if self._chrome == visible and not self._settings_open:
            self.update()
            return
        self._chrome = visible
        self._relayout("bottom")

    def _relayout(self, anchor: str) -> None:
        """Resize around the current top or bottom edge."""
        old = self.geometry()
        center_x = old.center().x()
        bottom = old.bottom()
        self._apply_size()
        x = center_x - self.width() // 2
        y = old.y() if anchor == "top" else bottom - self.height() + 1
        self.move(x, y)
        self.update()

    def _limits(self) -> tuple[int, int, int, int]:
        """Return min width, max width, min height, and max height for this monitor."""
        screen = self.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry()
        return (
            max(220, int(area.width() * 0.16)),
            int(area.width() * 0.62),
            max(56, int(area.height() * 0.045)),
            int(area.height() * 0.36),
        )

    def _apply_size(self) -> None:
        """Resize to the lyric text, clamped to the monitor limits."""
        min_w, max_w, min_h, max_h = self._limits()
        text_w, text_h = self._content_size()
        bar = _BAR_H if (self._chrome or self._settings_open) else 0
        settings = self._settings_height() if self._settings_open else 0
        notice = 28 if self._notice_key else 0
        width = int(max(min_w, min(max_w, text_w + _PAD_X * 2)))
        height = int(max(min_h, min(max_h, text_h + _PAD_Y * 2 + bar + settings + notice)))
        self.resize(width, height)

    def _content_size(self) -> tuple[float, float]:
        """Return the width and height of the lyric or candidate block."""
        metrics = QFontMetrics(self._lyric_font())
        line_h = metrics.height()
        if self._mode == "status":
            return metrics.horizontalAdvance(self.text(self._status_key or "loading")), line_h
        if self._mode == "choices":
            return self._choice_size(metrics, line_h)
        if self._line_mode == "one" or self._highlight == "single":
            shown = self.left if self._highlight != "right" else self.right
            return max(metrics.horizontalAdvance(shown), 1), line_h
        return (
            max(metrics.horizontalAdvance(self.left), metrics.horizontalAdvance(self.right), 1),
            line_h * 2 + 14,
        )

    def _choice_size(self, metrics: QFontMetrics, line_h: int) -> tuple[float, float]:
        """Return the candidate list's text size."""
        button_w = metrics.horizontalAdvance(self.text("use")) + 22
        widths = [metrics.horizontalAdvance(self.text("pick"))]
        for row in self._choice_rows:
            widths.append(min(metrics.horizontalAdvance(row.label), 560))
            widths.append(metrics.horizontalAdvance(row.url) + 12 + button_w)
        text_h = line_h + 10 + len(self._choice_rows) * (line_h * 2 + 18)
        return max(widths) if widths else 240, text_h

    def _body_rect(self) -> QRectF:
        """Return the rectangle under the bar where lyrics are drawn."""
        top = _BAR_H if (self._chrome or self._settings_open) else 0
        bottom = self._settings_height() if self._settings_open else 0
        if self._notice_key:
            bottom += 28
        return QRectF(0, top, self.width(), self.height() - top - bottom)

    def _settings_height(self) -> int:
        """Return the settings panel height."""
        return QFontMetrics(_ui_font(13)).height() * 3 + 36

    def _lyric_font(self) -> QFont:
        """Return the lyric font at the chosen size."""
        font = QFont("Segoe UI", self._font_size)
        font.setWeight(QFont.Weight.Normal)
        return font

    def _draw_bar(self, painter: QPainter) -> None:
        """Draw the centered icon row."""
        icons = [
            ("settings", _GLYPH["settings"]),
            ("lock", _GLYPH["unlock"] if self._on_top else _GLYPH["lock"]),
            ("previous", _GLYPH["previous"]),
            ("pause", _GLYPH["pause"] if self._playing else _GLYPH["play"]),
            ("next", _GLYPH["next"]),
            ("minimize", _GLYPH["minimize"]),
            ("close", _GLYPH["close"]),
        ]
        total = len(icons) * _ICON + (len(icons) - 1) * _ICON_GAP
        x = (self.width() - total) / 2
        y = (_BAR_H - _ICON) / 2
        painter.setFont(_icon_font(14))
        for kind, glyph in icons:
            rect = QRectF(x, y, _ICON, _ICON)
            if self._hot == f"{kind}:{kind}":
                painter.fillRect(rect, QColor(255, 255, 255, 50))
            self._draw_glyph(painter, rect, glyph)
            self._hits.append((rect, kind, kind))
            x += _ICON + _ICON_GAP

    def _draw_glyph(self, painter: QPainter, rect: QRectF, glyph: str) -> None:
        """Draw one icon in black with a light edge so it shows on any desktop."""
        flags = Qt.AlignmentFlag.AlignCenter
        painter.setPen(QColor(255, 255, 255, 200))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            painter.drawText(rect.translated(dx, dy), flags, glyph)
        painter.setPen(QColor(0, 0, 0))
        painter.drawText(rect, flags, glyph)

    def _draw_lyrics(self, painter: QPainter, body: QRectF) -> None:
        """Draw one centered line, or the left and right pair."""
        font = self._lyric_font()
        painter.setFont(font)
        metrics = QFontMetrics(font)
        content = body.adjusted(_PAD_X, _PAD_Y, -_PAD_X, -_PAD_Y)
        line_h = metrics.height()
        if self._line_mode == "one" or self._highlight == "single":
            shown = self.right if self._highlight == "right" else self.left
            top = content.y() + max(0, (content.height() - line_h) / 2)
            self._draw_marked(
                painter,
                QRectF(content.x(), top, content.width(), line_h),
                shown,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                current=True,
            )
            return
        block_h = line_h * 2 + 14
        top = content.y() + max(0, (content.height() - block_h) / 2)
        max_w = int(content.width())
        left = metrics.elidedText(self.left, Qt.TextElideMode.ElideRight, max_w)
        right = metrics.elidedText(self.right, Qt.TextElideMode.ElideRight, max_w)
        self._draw_marked(
            painter,
            QRectF(content.x(), top, content.width(), line_h),
            left,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            current=self._highlight == "left",
        )
        self._draw_marked(
            painter,
            QRectF(content.x(), top + line_h + 14, content.width(), line_h),
            right,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            current=self._highlight == "right",
        )

    def _draw_status(self, painter: QPainter, body: QRectF) -> None:
        """Draw one centered status sentence."""
        painter.setFont(self._lyric_font())
        self._draw_marked(
            painter,
            body.adjusted(_PAD_X, 0, -_PAD_X, 0),
            self.text(self._status_key or "loading"),
            Qt.AlignmentFlag.AlignCenter,
            current=True,
        )

    def _draw_marked(self, painter: QPainter, rect: QRectF, text: str, flags: Qt.AlignmentFlag, current: bool) -> None:
        """Draw lyric text. The current line is solid. The other line is lighter."""
        if not text:
            return
        painter.setPen(QColor(255, 255, 255, 210))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            painter.drawText(rect.translated(dx, dy), flags, text)
        painter.setPen(QColor(0, 0, 0) if current else QColor(70, 70, 70, 170))
        painter.drawText(rect, flags, text)

    def _draw_choices(self, painter: QPainter, bounds: QRectF) -> None:
        """Draw each candidate on the left, with a link and a Use this button."""
        font = self._lyric_font()
        metrics = QFontMetrics(font)
        line_h = metrics.height()
        use = self.text("use")
        button_w = metrics.horizontalAdvance(use) + 22
        button_h = line_h + 6
        content = bounds.adjusted(_PAD_X, 8, -_PAD_X, -8)
        painter.setFont(font)
        y = content.y()
        self._draw_marked(
            painter,
            QRectF(content.x(), y, content.width(), line_h),
            self.text("pick"),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            current=True,
        )
        y += line_h + 10
        for row in self._choice_rows:
            label = metrics.elidedText(row.label, Qt.TextElideMode.ElideRight, int(content.width()))
            self._draw_marked(
                painter,
                QRectF(content.x(), y, content.width(), line_h),
                label,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                current=True,
            )
            y += line_h + 4
            link_w = metrics.horizontalAdvance(row.url)
            link_rect = QRectF(content.x(), y, link_w, line_h)
            painter.setPen(QColor(20, 70, 160))
            painter.drawText(link_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, row.url)
            if self._hot == f"link:{row.url}":
                painter.drawLine(QPointF(link_rect.left(), link_rect.bottom() - 1), QPointF(link_rect.right(), link_rect.bottom() - 1))
            self._hits.append((link_rect, "link", row.url))
            button = QRectF(link_rect.right() + 12, y - 3, button_w, button_h)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 210 if self._hot == f"use:{row.record_id}" else 160))
            painter.drawRoundedRect(button, 8, 8)
            painter.setPen(QColor(0, 0, 0))
            painter.drawText(button, Qt.AlignmentFlag.AlignCenter, use)
            self._hits.append((button, "use", row.record_id))
            y += line_h + 14

    def _draw_settings(self, painter: QPainter) -> None:
        """Draw language, line mode, and font size under the lyrics."""
        font = _ui_font(13)
        metrics = QFontMetrics(font)
        line_h = metrics.height()
        top = self.height() - self._settings_height() - (28 if self._notice_key else 0)
        y = top + 8
        painter.setFont(font)
        rows = [
            ("lang", self.text("language"), self.text("language_value")),
            ("lines", self.text("lines"), self.text(self._line_mode)),
        ]
        for kind, label, value in rows:
            rect = QRectF(_PAD_X, y, self.width() - _PAD_X * 2, line_h + 6)
            if self._hot == f"{kind}:{kind}":
                painter.fillRect(rect, QColor(255, 255, 255, 40))
            painter.setPen(QColor(0, 0, 0))
            painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            painter.drawText(rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, value)
            self._hits.append((rect, kind, kind))
            y += line_h + 10
        label_rect = QRectF(_PAD_X, y, self.width() * 0.45, line_h + 6)
        painter.setPen(QColor(0, 0, 0))
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text("font"))
        minus = QRectF(self.width() - _PAD_X - 92, y, 24, line_h + 6)
        plus = QRectF(self.width() - _PAD_X - 24, y, 24, line_h + 6)
        value_rect = QRectF(minus.right(), y, plus.left() - minus.right(), line_h + 6)
        for rect, kind, glyph in ((minus, "font-down", "−"), (plus, "font-up", "+")):
            if self._hot == f"{kind}:{kind}":
                painter.fillRect(rect, QColor(255, 255, 255, 60))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyph)
            self._hits.append((rect, kind, kind))
        painter.drawText(value_rect, Qt.AlignmentFlag.AlignCenter, str(self._font_size))

    def _draw_notice(self, painter: QPainter) -> None:
        """Draw the playback message along the bottom."""
        painter.setFont(_ui_font(11))
        rect = QRectF(_PAD_X, self.height() - 26, self.width() - _PAD_X * 2, 20)
        painter.setPen(QColor(0, 0, 0))
        painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text(self._notice_key))

    def _hit_at(self, pos: QPointF) -> tuple[str, str | int] | None:
        """Return the control under pos."""
        for rect, kind, payload in self._hits:
            if rect.contains(pos):
                return kind, payload
        return None


def main() -> None:
    """Open a sample overlay so the hover bar and the two lyric lines can be seen.

    Purpose:
        Show the window without Spotify.

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
    window = LyricOverlay("歌词第一行", "歌词第二行", "left")
    frame = window.frameGeometry()
    screen = app.primaryScreen().availableGeometry().center()
    frame.moveCenter(screen)
    window.move(frame.topLeft())
    window.show()
    window.raise_()
    window.activateWindow()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
