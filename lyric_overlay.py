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
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QTextLayout,
)
from PyQt6.QtWidgets import QApplication, QWidget

from lrclib_client import LyricCandidate

_SETTINGS_PATH = Path(__file__).resolve().parent / ".spotify" / "overlay_ui.json"
_TEXT_DIR = Path(__file__).resolve().parent / "settings_text"
_SCROLL_W = 10
_FONT_MIN = 16
_FONT_MAX = 48
_FONT_DEFAULT = 26
_HOVER_ALPHA = 26
_PAD_X = 28
_PAD_Y = 18
_BAR_H = 40
_ICON = 28
_ICON_GAP = 10
# The overlay width stays at half the monitor so a longer line does not move the window.
_WIDTH_RATIO = 0.5
_HEIGHT_RATIO = 0.5

def _load_text() -> dict[str, dict[str, str]]:
    """Load one overlay-string table per JSON file in settings_text.

    Purpose:
        Each file name is a language code. The file holds interface strings
        only. Song lyrics stay outside this folder.

    Parameters:
        None.

    Returns:
        dict[str, dict[str, str]]: Language code to string table. English is
        always present, even if its file cannot be read.

    Precondition:
        settings_text sits next to this module.

    Postcondition:
        Missing or broken files are skipped. A missing English file yields
        an empty English table so lookups still fall back to the key.
    """
    tables: dict[str, dict[str, str]] = {}
    if _TEXT_DIR.is_dir():
        for path in sorted(_TEXT_DIR.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            strings = {str(key): str(value) for key, value in raw.items() if isinstance(value, str)}
            if strings:
                tables[path.stem] = strings
    tables.setdefault("en", {})
    return tables


_TEXT = _load_text()

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
    """Return the saved language, line mode, font size, and theme."""
    saved = {
        "language": "en",
        "lines": "two",
        "font": _FONT_DEFAULT,
        "theme": "dark",
        "script": "original",
        "store_local": False,
    }
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
    if raw.get("theme") in ("dark", "light"):
        saved["theme"] = raw["theme"]
    if raw.get("script") in ("original", "simplified", "traditional"):
        saved["script"] = raw["script"]
    if isinstance(raw.get("store_local"), bool):
        saved["store_local"] = raw["store_local"]
    return saved


def _save_ui(language: str, lines: str, font: int, theme: str, script: str, store_local: bool) -> None:
    """Write the overlay settings next to the other local Spotify files."""
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(
        json.dumps(
            {
                "language": language,
                "lines": lines,
                "font": font,
                "theme": theme,
                "script": script,
                "store_local": store_local,
            },
            indent=2,
        ),
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
        self._theme = ui["theme"]
        self._script = ui["script"]
        self._store_local = ui["store_local"]
        self.left = left
        self.right = right
        self._highlight = highlight
        self._status_key = ""
        self._status_detail = ""
        self._notice_key = ""
        self._mode = "lyrics"
        self._words: tuple[tuple[str, float], ...] = ()
        self._word_next = ""
        self._choice_rows: list[LyricCandidate] = []
        self._choice_title = "pick"
        self._on_pick = None
        self._on_previous = None
        self._on_toggle = None
        self._on_next = None
        self._playing = True
        self._on_top = True
        self._chrome = False
        self._settings_open = False
        self._lang_open = False
        self._scroll_drag: tuple[float, int, float] | None = None
        self._hits: list[tuple[QRectF, str, str | int]] = []
        self._drag_offset: QPoint | None = None
        self._hot: str | None = None
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._clear_notice)
        self._chrome_timer = QTimer(self)
        self._chrome_timer.setSingleShot(True)
        self._chrome_timer.timeout.connect(self._hide_chrome_if_outside)
        self._settings_scroll = 0
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

    def _status_sentence(self) -> str:
        """Return the status line, including a NetEase detail when one exists."""
        sentence = self.text(self._status_key or "loading")
        detail = self._status_detail.strip()
        if not detail:
            return sentence
        return f"{sentence} {detail}"

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

    def set_status(self, key: str, detail: str = "") -> None:
        """Replace the lyrics with one translated status line.

        Parameters:
            key (str): A key in the overlay text table.
            detail (str): Extra words from a NetEase response, shown after
                the translated line. Empty hides that extra part.
        """
        self._mode = "status"
        self._status_key = key
        self._status_detail = detail
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

    def set_words(self, words: tuple[tuple[str, float], ...] | list[tuple[str, float]], following: str, side: str) -> None:
        """Show a NetEase line and fill each word from the left as it is sung.

        Purpose:
            Each token carries how much of its duration has elapsed, from 0
            to 1. A fill that changes inside the same line repaints without
            moving the window.

        Parameters:
            words: Tokens of the current line as (text, fill).
            following (str): The next line, drawn without a word highlight.
            side (str): "left" or "right", the slot that holds the current line.

        Returns:
            None.

        Precondition:
            words is not empty. Each fill is between 0 and 1.

        Postcondition:
            The window size matches the new line. A fill-only change does
            not move the window.
        """
        packed = tuple((text, float(fill)) for text, fill in words)
        same_line = (
            self._mode == "words"
            and self._highlight == side
            and self._word_next == following
            and tuple(text for text, _fill in self._words) == tuple(text for text, _fill in packed)
        )
        self._mode = "words"
        self._words = packed
        self._word_next = following
        self._highlight = side
        self._status_key = ""
        self._choice_rows = []
        self._on_pick = None
        current = "".join(text for text, _fill in self._words)
        if side == "right":
            self.left = following
            self.right = current
        else:
            self.left = current
            self.right = following
        if same_line:
            self.update()
            return
        self._relayout("top")

    def set_choices(self, candidates: list[LyricCandidate], on_pick, title_key: str = "pick") -> None:
        """Show preview links and a Use this button for each candidate.

        Purpose:
            Let a shortcut user preview a match in the browser and choose it.
            Lyric lines themselves stay plain text.

        Parameters:
            candidates (list[LyricCandidate]): Up to five synced records.
            on_pick: Called with the record id when Use this is clicked.
            title_key (str): Overlay text key for the list heading.

        Returns:
            None.

        Precondition:
            candidates is non-empty.

        Postcondition:
            The window is tall enough for every candidate. Links open in the browser.
        """
        self._mode = "choices"
        self._choice_rows = list(candidates)
        self._choice_title = title_key
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
            painter.fillRect(self.rect(), self._panel_color())
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
        """Reveal the bar and the theme tint."""
        self._chrome_timer.stop()
        self._set_chrome(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Hide the bar after the pointer has actually left the window."""
        self._hot = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._chrome_timer.start(120)
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Scroll the settings list when it is open and the pointer is over it."""
        if not self._settings_open:
            super().wheelEvent(event)
            return
        view = self._settings_view_rect()
        if not view.contains(event.position()):
            super().wheelEvent(event)
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        step = 28 if delta > 0 else -28
        limit = self._settings_max_scroll()
        self._settings_scroll = max(0, min(limit, self._settings_scroll - step))
        self.update()

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
        if self._settings_open and self._begin_scroll_drag(event.position()):
            return
        hit = self._hit_at(event.position())
        if hit is not None:
            self._activate(hit)
            return
        if self._lang_open:
            self._lang_open = False
            self.update()
            return
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 — Qt override name
        """Move the window, or point at a control."""
        if self._scroll_drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._move_scroll_drag(event.position().y())
            return
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
        self._scroll_drag = None
        super().mouseReleaseEvent(event)

    def _activate(self, hit: tuple[str, str | int]) -> None:
        """Carry out one bar, settings, or candidate click."""
        kind, payload = hit
        if kind == "settings":
            self._settings_open = not self._settings_open
            self._lang_open = False
            self._scroll_drag = None
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
            self._lang_open = not self._lang_open
            self.update()
            return
        if kind == "pick-lang":
            code = str(payload)
            if code in _TEXT:
                self._language = code
                self._persist()
            self._lang_open = False
            self._relayout("top")
            return
        if kind == "lang-menu":
            return
        if kind == "lines":
            self._lang_open = False
            self._line_mode = "one" if self._line_mode == "two" else "two"
            self._persist()
            self._relayout("top")
            return
        if kind == "theme":
            self._lang_open = False
            self._theme = "light" if self._theme == "dark" else "dark"
            self._persist()
            self.update()
            return
        if kind == "script":
            self._lang_open = False
            order = ("original", "simplified", "traditional")
            self._script = order[(order.index(self._script) + 1) % len(order)]
            self._persist()
            self._relayout("top")
            return
        if kind == "store":
            self._lang_open = False
            self._store_local = not self._store_local
            self._persist()
            self.update()
            return
        if kind == "font-down":
            self._lang_open = False
            self._font_size = max(_FONT_MIN, self._font_size - 2)
            self._persist()
            self._relayout("top")
            return
        if kind == "font-up":
            self._lang_open = False
            self._font_size = min(_FONT_MAX, self._font_size + 2)
            self._persist()
            self._relayout("top")
            return
        if kind == "link":
            QDesktopServices.openUrl(QUrl(str(payload)))
            return
        if kind == "use" and self._on_pick is not None:
            self._on_pick(int(payload))

    def chinese_script(self) -> str:
        """Return original, simplified, or traditional for lyric characters."""
        return self._script

    def _persist(self) -> None:
        """Save language, line mode, font size, theme, and Chinese script."""
        _save_ui(self._language, self._line_mode, self._font_size, self._theme, self._script, self._store_local)

    def _hide_chrome_if_outside(self) -> None:
        """Drop the bar only when the pointer is outside the window."""
        if self._settings_open:
            self.update()
            return
        if self.frameGeometry().contains(QCursor.pos()):
            return
        self._set_chrome(False)

    def _panel_color(self) -> QColor:
        """Return the hover fill. Dark is black. Light is white."""
        if self._theme == "light":
            return QColor(255, 255, 255, 210)
        return QColor(0, 0, 0, 150)

    def _ink(self) -> QColor:
        """Return the settings and icon color for the current theme."""
        if self._theme == "light":
            return QColor(20, 20, 20)
        return QColor(255, 255, 255)

    def _pair_inset(self) -> int:
        """Return the gap after line one and before line two."""
        return max(32, int(self._font_size * 1.8))

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

    def _screen_area(self):
        """Return the working area of the monitor under this window."""
        screen = self.screen() or QApplication.primaryScreen()
        return screen.availableGeometry()

    def _fixed_width(self) -> int:
        """Return the overlay width: half the monitor, every song."""
        return int(self._screen_area().width() * _WIDTH_RATIO)

    def _max_window_height(self) -> int:
        """Return the tallest the overlay may be: half the monitor."""
        return int(self._screen_area().height() * _HEIGHT_RATIO)

    def _relayout(self, anchor: str) -> None:
        """Resize around the current top or bottom edge.

        Purpose:
            Keep the window where the user left it. A word highlight does
            not change the size, so the window is not moved again.

        Parameters:
            anchor (str): "top" keeps the top edge. Anything else keeps the
                bottom edge.

        Returns:
            None.

        Precondition:
            The widget has been constructed.

        Postcondition:
            An unchanged size leaves the window position unchanged.
        """
        old = self.geometry()
        self._apply_size()
        x = old.x() + (old.width() - self.width()) // 2
        if anchor == "top":
            y = old.y()
        else:
            y = old.y() + old.height() - self.height()
        if x != old.x() or y != old.y():
            self.move(x, y)
        self.update()

    def _apply_size(self) -> None:
        """Keep the width at half the monitor. Height follows wrapped lines, up to half the screen."""
        text_h = self._content_size()[1]
        bar = _BAR_H if (self._chrome or self._settings_open) else 0
        settings = self._settings_view_height() if self._settings_open else 0
        notice = 28 if self._notice_key else 0
        height = int(text_h + _PAD_Y * 2 + bar + settings + notice)
        height = min(max(height, 80), self._max_window_height())
        self.resize(self._fixed_width(), height)

    def _lyric_column_width(self) -> int:
        """Return the width one lyric slot may use before it wraps."""
        inner = self._fixed_width() - _PAD_X * 2
        if self._line_mode == "one" or self._highlight == "single" or self._mode != "lyrics":
            return max(1, inner)
        return max(1, inner - self._pair_inset())

    def _visual_lines(self, text: str, width: int) -> list[str]:
        """Break text into rows that fit width. Long lines wrap instead of ending in an ellipsis."""
        if not text:
            return []
        layout = QTextLayout(text, self._lyric_font())
        layout.beginLayout()
        rows: list[str] = []
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(max(1, width))
            rows.append(text[line.textStart() : line.textStart() + line.textLength()])
        layout.endLayout()
        return rows or [text]

    def _content_size(self) -> tuple[float, float]:
        """Return the lyric block height inside the fixed window width."""
        metrics = QFontMetrics(self._lyric_font())
        line_h = metrics.height()
        if self._mode == "status":
            rows = self._visual_lines(self._status_sentence(), self._lyric_column_width())
            return self._fixed_width(), max(1, len(rows)) * line_h
        if self._mode == "words":
            return self._fixed_width(), self._word_block_height(metrics, line_h)
        if self._mode == "choices":
            return self._choice_size(metrics, line_h)
        width = self._lyric_column_width()
        if self._line_mode == "one" or self._highlight == "single":
            shown = self.right if self._highlight == "right" else self.left
            rows = self._visual_lines(shown, width)
            return self._fixed_width(), max(1, len(rows)) * line_h
        left_n = max(1, len(self._visual_lines(self.left, width))) if self.left else 0
        right_n = max(1, len(self._visual_lines(self.right, width))) if self.right else 0
        gap = 14 if left_n and right_n else 0
        return self._fixed_width(), left_n * line_h + gap + right_n * line_h

    def _choice_size(self, metrics: QFontMetrics, line_h: int) -> tuple[float, float]:
        """Return the candidate list's text size."""
        button_w = metrics.horizontalAdvance(self.text("use")) + 22
        widths = [metrics.horizontalAdvance(self.text(self._choice_title))]
        for row in self._choice_rows:
            widths.append(min(metrics.horizontalAdvance(row.label), 560))
            widths.append(metrics.horizontalAdvance(row.url) + 12 + button_w)
        text_h = line_h + 10 + len(self._choice_rows) * (line_h * 2 + 18)
        return max(widths) if widths else 240, text_h

    def _body_rect(self) -> QRectF:
        """Return the rectangle under the bar where lyrics are drawn."""
        top = _BAR_H if (self._chrome or self._settings_open) else 0
        bottom = self._settings_view_height() if self._settings_open else 0
        if self._notice_key:
            bottom += 28
        return QRectF(0, top, self.width(), self.height() - top - bottom)

    def _settings_view_height(self) -> int:
        """Return the fixed height of the settings panel on screen."""
        line_h = QFontMetrics(_ui_font(13)).height()
        return line_h * 3 + 36

    def _settings_content_height(self) -> int:
        """Return the full settings list height, including rows below the panel."""
        line_h = QFontMetrics(_ui_font(13)).height()
        return 8 + (line_h + 10) * 5 + (line_h + 6) + 8

    def _settings_max_scroll(self) -> int:
        """Return how far the settings list can move up."""
        return max(0, self._settings_content_height() - self._settings_view_height())

    def _settings_view_rect(self) -> QRectF:
        """Return the on-screen rectangle of the settings panel."""
        top = self.height() - self._settings_view_height() - (28 if self._notice_key else 0)
        return QRectF(0, top, self.width(), self._settings_view_height())

    def _lyric_font(self) -> QFont:
        """Return a bold lyric face. Chinese uses Microsoft YaHei when it is installed."""
        font = QFont("Microsoft YaHei", self._font_size)
        font.setWeight(QFont.Weight.Bold)
        if not QFontMetrics(font).inFontUcs4(0x6B4C):
            font = QFont("Segoe UI", self._font_size)
            font.setWeight(QFont.Weight.Bold)
        return font

    def _draw_bar(self, painter: QPainter) -> None:
        """Draw the centered icon row."""
        icons = [
            ("settings", _GLYPH["settings"]),
            ("lock", _GLYPH["lock"] if self._on_top else _GLYPH["unlock"]),
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
                tint = QColor(255, 255, 255, 50) if self._theme == "dark" else QColor(0, 0, 0, 28)
                painter.fillRect(rect, tint)
            self._draw_glyph(painter, rect, glyph)
            self._hits.append((rect, kind, kind))
            x += _ICON + _ICON_GAP

    def _draw_glyph(self, painter: QPainter, rect: QRectF, glyph: str) -> None:
        """Draw one icon in the theme color with a contrasting edge."""
        flags = Qt.AlignmentFlag.AlignCenter
        edge = QColor(0, 0, 0) if self._theme == "dark" else QColor(255, 255, 255)
        painter.setPen(edge)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            painter.drawText(rect.translated(dx, dy), flags, glyph)
        painter.setPen(self._ink())
        painter.drawText(rect, flags, glyph)

    def _pack_words(
        self,
        words: tuple[tuple[str, float], ...],
        width: int,
        metrics: QFontMetrics,
    ) -> list[list[tuple[str, float, int]]]:
        """Break a word line into rows that fit width."""
        rows: list[list[tuple[str, float, int]]] = []
        row: list[tuple[str, float, int]] = []
        used = 0
        for text, fill in words:
            advance = metrics.horizontalAdvance(text)
            if row and used + advance > width:
                rows.append(row)
                row = []
                used = 0
            row.append((text, fill, advance))
            used += advance
        if row:
            rows.append(row)
        return rows or [[("", 0.0, 0)]]

    def _word_block_height(self, metrics: QFontMetrics, line_h: int) -> float:
        """Return the height of the current word line plus the following line."""
        content_w = max(1, self._fixed_width() - _PAD_X * 2)
        if self._line_mode == "one":
            rows = self._pack_words(self._words, content_w, metrics)
            return max(1, len(rows)) * line_h
        col_w = max(1, content_w - self._pair_inset())
        word_rows = self._pack_words(self._words, col_w, metrics)
        other = self._word_next
        other_n = max(1, len(self._visual_lines(other, col_w))) if other else 0
        gap = 14 if word_rows and other_n else 0
        return len(word_rows) * line_h + gap + other_n * line_h

    def _draw_word_rows(
        self,
        painter: QPainter,
        x: float,
        y: float,
        width: float,
        line_h: int,
        words: tuple[tuple[str, float], ...],
        align: str,
    ) -> None:
        """Draw one lyric line, filling each word from the left by its elapsed duration."""
        metrics = painter.fontMetrics()
        rows = self._pack_words(words, int(width), metrics)
        for row in rows:
            row_w = sum(item[2] for item in row)
            if align == "right":
                cursor = x + width - row_w
            elif align == "center":
                cursor = x + max(0, (width - row_w) / 2)
            else:
                cursor = x
            for text, fill, advance in row:
                self._draw_marked(
                    painter,
                    QRectF(cursor, y, max(1, advance), line_h),
                    text,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    current=False,
                    fill=fill,
                )
                cursor += advance
            y += line_h

    def _draw_lyrics(self, painter: QPainter, body: QRectF) -> None:
        """Draw one centered line, or the left and right pair."""
        if self._mode == "words":
            self._draw_word_lyrics(painter, body)
            return
        font = self._lyric_font()
        painter.setFont(font)
        metrics = QFontMetrics(font)
        content = body.adjusted(_PAD_X, _PAD_Y, -_PAD_X, -_PAD_Y)
        line_h = metrics.height()
        painter.save()
        painter.setClipRect(body)
        if self._line_mode == "one" or self._highlight == "single":
            shown = self.right if self._highlight == "right" else self.left
            rows = self._visual_lines(shown, int(content.width()))
            block_h = max(1, len(rows)) * line_h
            top = content.y() + max(0, (content.height() - block_h) / 2)
            self._draw_wrapped(
                painter,
                content.x(),
                top,
                content.width(),
                line_h,
                rows,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                current=True,
            )
            painter.restore()
            return
        inset = self._pair_inset()
        col_w = max(1.0, content.width() - inset)
        left_rows = self._visual_lines(self.left, int(col_w))
        right_rows = self._visual_lines(self.right, int(col_w))
        gap = 14 if left_rows and right_rows else 0
        block_h = len(left_rows) * line_h + gap + len(right_rows) * line_h
        top = content.y() + max(0, (content.height() - block_h) / 2)
        self._draw_wrapped(
            painter,
            content.x(),
            top,
            col_w,
            line_h,
            left_rows,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            current=self._highlight == "left",
        )
        self._draw_wrapped(
            painter,
            content.x() + inset,
            top + len(left_rows) * line_h + gap,
            col_w,
            line_h,
            right_rows,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            current=self._highlight == "right",
        )
        painter.restore()

    def _draw_word_lyrics(self, painter: QPainter, body: QRectF) -> None:
        """Draw the NetEase line with one highlighted word, plus the next line."""
        font = self._lyric_font()
        painter.setFont(font)
        metrics = QFontMetrics(font)
        content = body.adjusted(_PAD_X, _PAD_Y, -_PAD_X, -_PAD_Y)
        line_h = metrics.height()
        painter.save()
        painter.setClipRect(body)
        if self._line_mode == "one":
            rows = self._pack_words(self._words, int(content.width()), metrics)
            block_h = max(1, len(rows)) * line_h
            top = content.y() + max(0, (content.height() - block_h) / 2)
            self._draw_word_rows(painter, content.x(), top, content.width(), line_h, self._words, "center")
            painter.restore()
            return
        inset = self._pair_inset()
        col_w = max(1.0, content.width() - inset)
        word_rows = self._pack_words(self._words, int(col_w), metrics)
        other_rows = self._visual_lines(self._word_next, int(col_w)) if self._word_next else []
        gap = 14 if word_rows and other_rows else 0
        block_h = len(word_rows) * line_h + gap + len(other_rows) * line_h
        top = content.y() + max(0, (content.height() - block_h) / 2)
        if self._highlight == "right":
            self._draw_wrapped(
                painter,
                content.x(),
                top,
                col_w,
                line_h,
                other_rows,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                current=False,
            )
            self._draw_word_rows(
                painter,
                content.x() + inset,
                top + len(other_rows) * line_h + gap,
                col_w,
                line_h,
                self._words,
                "right",
            )
        else:
            self._draw_word_rows(painter, content.x(), top, col_w, line_h, self._words, "left")
            self._draw_wrapped(
                painter,
                content.x() + inset,
                top + len(word_rows) * line_h + gap,
                col_w,
                line_h,
                other_rows,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                current=False,
            )
        painter.restore()

    def _draw_wrapped(
        self,
        painter: QPainter,
        x: float,
        y: float,
        width: float,
        line_h: int,
        rows: list[str],
        flags: Qt.AlignmentFlag,
        current: bool,
    ) -> None:
        """Draw each wrapped row of one lyric slot."""
        for index, row in enumerate(rows):
            self._draw_marked(
                painter,
                QRectF(x, y + index * line_h, width, line_h),
                row,
                flags,
                current,
            )

    def _draw_status(self, painter: QPainter, body: QRectF) -> None:
        """Draw one centered status sentence."""
        font = self._lyric_font()
        painter.setFont(font)
        content = body.adjusted(_PAD_X, _PAD_Y, -_PAD_X, -_PAD_Y)
        line_h = QFontMetrics(font).height()
        rows = self._visual_lines(self._status_sentence(), int(content.width()))
        block_h = max(1, len(rows)) * line_h
        top = content.y() + max(0, (content.height() - block_h) / 2)
        self._draw_wrapped(
            painter,
            content.x(),
            top,
            content.width(),
            line_h,
            rows,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            current=False,
        )

    def _draw_marked(
        self,
        painter: QPainter,
        rect: QRectF,
        text: str,
        flags: Qt.AlignmentFlag,
        current: bool,
        fill: float | None = None,
    ) -> None:
        """Draw lyric text with a black outline. Sung color wipes from the left.

        Purpose:
            A full sung line uses the yellow-to-orange gradient. A NetEase
            word uses fill, from 0 to 1, so the left portion is the left
            crop of that gradient and the right portion stays white.

        Parameters:
            painter (QPainter): Active painter.
            rect (QRectF): Slot that contains the text.
            text (str): Characters to draw.
            flags (Qt.AlignmentFlag): Horizontal alignment inside rect.
            current (bool): True paints the whole string in the sung gradient.
            fill (float | None): Portion of the glyphs to cover, from the
                left. None follows current.

        Returns:
            None.

        Precondition:
            fill is None or between 0 and 1.

        Postcondition:
            The painter's clip is unchanged.
        """
        if not text:
            return
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text)
        if flags & Qt.AlignmentFlag.AlignHCenter:
            x = rect.x() + (rect.width() - width) / 2
        elif flags & Qt.AlignmentFlag.AlignRight:
            x = rect.right() - width
        else:
            x = rect.x()
        baseline = rect.y() + metrics.ascent() + (rect.height() - metrics.height()) / 2
        path = QPainterPath()
        path.addText(x, baseline, painter.font(), text)
        outline = QPen(QColor(0, 0, 0), max(2.0, self._font_size * 0.16))
        outline.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        outline.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.strokePath(path, outline)
        amount = 1.0 if current else 0.0
        if fill is not None:
            amount = max(0.0, min(1.0, fill))
        if amount < 1.0:
            painter.fillPath(path, QColor(255, 255, 255))
        if amount <= 0.0:
            return
        bounds = path.boundingRect()
        if amount >= 1.0 or bounds.width() <= 0:
            painter.fillPath(path, self._sung_gradient(bounds))
            return
        self._paint_partial_fill(painter, path, bounds, amount)

    def _sung_gradient(self, bounds: QRectF) -> QLinearGradient:
        """Return the yellow-to-orange fill for one glyph's full height."""
        gradient = QLinearGradient(bounds.topLeft(), bounds.bottomLeft())
        gradient.setColorAt(0.0, QColor(255, 244, 140))
        gradient.setColorAt(0.35, QColor(255, 196, 64))
        gradient.setColorAt(0.7, QColor(255, 140, 24))
        gradient.setColorAt(1.0, QColor(255, 98, 16))
        return gradient

    def _paint_partial_fill(self, painter: QPainter, path: QPainterPath, bounds: QRectF, amount: float) -> None:
        """Paint the left portion of the sung word, fading into the white side.

        Purpose:
            Build the fully highlighted glyphs, then hide the right side with
            a short horizontal fade. The edge moves with amount instead of
            cutting the letter on a hard line.

        Parameters:
            painter (QPainter): Window painter. The white word is already drawn.
            path (QPainterPath): Glyph outlines in window coordinates.
            bounds (QRectF): Tight bounds of those glyphs.
            amount (float): Portion to reveal, from 0 to 1.

        Returns:
            None.

        Precondition:
            0 < amount < 1 and bounds has a positive width.

        Postcondition:
            The window painter's clip and transform are unchanged.
        """
        pad = 4.0
        ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
        ratio = max(1.0, ratio)
        logical = QRectF(0, 0, bounds.width() + pad * 2, bounds.height() + pad * 2)
        image = QImage(
            max(1, int(logical.width() * ratio)),
            max(1, int(logical.height() * ratio)),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.setDevicePixelRatio(ratio)
        image.fill(0)
        part = QPainter(image)
        part.setRenderHint(QPainter.RenderHint.Antialiasing)
        part.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        shifted = path.translated(-bounds.left() + pad, -bounds.top() + pad)
        glyph_bounds = QRectF(pad, pad, bounds.width(), bounds.height())
        part.fillPath(shifted, self._sung_gradient(glyph_bounds))
        feather = min(12.0, max(4.0, bounds.width() * 0.18))
        cut = pad + bounds.width() * amount
        fade = QLinearGradient(cut - feather, 0, cut, 0)
        fade.setColorAt(0.0, QColor(0, 0, 0, 255))
        fade.setColorAt(1.0, QColor(0, 0, 0, 0))
        part.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        part.fillRect(logical, fade)
        part.end()
        painter.drawImage(QPointF(bounds.left() - pad, bounds.top() - pad), image)

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
            self.text(self._choice_title),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            current=False,
        )
        y += line_h + 10
        for row in self._choice_rows:
            label = metrics.elidedText(row.label, Qt.TextElideMode.ElideRight, int(content.width()))
            self._draw_marked(
                painter,
                QRectF(content.x(), y, content.width(), line_h),
                label,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                current=False,
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
        """Draw language, line mode, theme, and font size in a scrolling panel."""
        font = _ui_font(13)
        metrics = QFontMetrics(font)
        line_h = metrics.height()
        view = self._settings_view_rect()
        self._settings_scroll = max(0, min(self._settings_max_scroll(), self._settings_scroll))
        y = view.y() + 8 - self._settings_scroll
        right_pad = _PAD_X + _SCROLL_W + 8
        painter.save()
        painter.setClipRect(view)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255, 40) if self._theme == "dark" else QColor(0, 0, 0, 28))
        painter.drawLine(QPointF(view.left() + 12, view.top()), QPointF(view.right() - 12, view.top()))
        rows = [
            ("lang", self.text("language"), f"{self.text('language_value')}  ▾"),
            ("lines", self.text("lines"), self.text(self._line_mode)),
            ("theme", self.text("theme"), self.text(self._theme)),
            ("script", self.text("script"), self.text(f"script_{self._script}")),
            ("store", self.text("store_local"), self.text("store_yes" if self._store_local else "store_no")),
        ]
        ink = self._ink()
        hot = QColor(255, 255, 255, 40) if self._theme == "dark" else QColor(0, 0, 0, 24)
        lang_row: QRectF | None = None
        for kind, label, value in rows:
            rect = QRectF(_PAD_X, y, self.width() - _PAD_X - right_pad, line_h + 6)
            if self._hot == f"{kind}:{kind}" or (kind == "lang" and self._lang_open):
                painter.fillRect(rect, hot)
            painter.setPen(ink)
            painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            painter.drawText(rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, value)
            if view.intersects(rect):
                self._hits.append((rect, kind, kind))
            if kind == "lang":
                lang_row = rect
            y += line_h + 10
        label_rect = QRectF(_PAD_X, y, self.width() * 0.45, line_h + 6)
        painter.setPen(ink)
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text("font"))
        minus = QRectF(self.width() - right_pad - 92, y, 24, line_h + 6)
        plus = QRectF(self.width() - right_pad - 24, y, 24, line_h + 6)
        value_rect = QRectF(minus.right(), y, plus.left() - minus.right(), line_h + 6)
        for rect, kind, glyph in ((minus, "font-down", "−"), (plus, "font-up", "+")):
            if self._hot == f"{kind}:{kind}":
                painter.fillRect(rect, hot)
            painter.setPen(ink)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyph)
            if view.intersects(rect):
                self._hits.append((rect, kind, kind))
        painter.setPen(ink)
        painter.drawText(value_rect, Qt.AlignmentFlag.AlignCenter, str(self._font_size))
        bar = self._settings_scrollbar()
        if bar is not None:
            track, thumb, _travel = bar
            painter.setPen(Qt.PenStyle.NoPen)
            track_ink = QColor(255, 255, 255, 36) if self._theme == "dark" else QColor(0, 0, 0, 28)
            thumb_ink = QColor(255, 255, 255, 170) if self._theme == "dark" else QColor(0, 0, 0, 120)
            painter.setBrush(track_ink)
            painter.drawRoundedRect(track, 2, 2)
            painter.setBrush(thumb_ink)
            painter.drawRoundedRect(thumb, 3, 3)
        painter.restore()
        if self._lang_open and lang_row is not None:
            self._draw_language_menu(painter, lang_row, font, metrics, line_h)

    def _draw_language_menu(
        self,
        painter: QPainter,
        anchor: QRectF,
        font: QFont,
        metrics: QFontMetrics,
        line_h: int,
    ) -> None:
        """Draw the list of languages from settings_text."""
        codes = self._language_codes()
        names = [(_TEXT[code].get("language_value") or code) for code in codes]
        row_h = line_h + 8
        menu_w = max(metrics.horizontalAdvance(name) for name in names) + 36
        menu_w = max(menu_w, 140)
        menu_h = row_h * len(codes) + 8
        left = min(max(8, anchor.right() - menu_w), self.width() - menu_w - 8)
        top = anchor.bottom() + 4
        if top + menu_h > self.height() - 8:
            top = max(8, anchor.top() - 4 - menu_h)
        menu = QRectF(left, top, menu_w, menu_h)
        painter.save()
        painter.setFont(font)
        painter.setPen(Qt.PenStyle.NoPen)
        fill = QColor(28, 28, 28, 235) if self._theme == "dark" else QColor(255, 255, 255, 242)
        painter.setBrush(fill)
        painter.drawRoundedRect(menu, 8, 8)
        painter.setPen(QColor(255, 255, 255, 50) if self._theme == "dark" else QColor(0, 0, 0, 40))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(menu.adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        self._hits.append((menu, "lang-menu", "menu"))
        ink = QColor(255, 255, 255) if self._theme == "dark" else QColor(20, 20, 20)
        y = menu.top() + 4
        for code, name in zip(codes, names):
            row = QRectF(menu.left() + 4, y, menu.width() - 8, row_h)
            if code == self._language or self._hot == f"pick-lang:{code}":
                tint = QColor(255, 255, 255, 28) if self._theme == "dark" else QColor(0, 0, 0, 16)
                painter.fillRect(row, tint)
            painter.setPen(ink)
            painter.drawText(
                row.adjusted(10, 0, -10, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                name,
            )
            self._hits.append((row, "pick-lang", code))
            y += row_h
        painter.restore()

    def _draw_notice(self, painter: QPainter) -> None:
        """Draw the playback message along the bottom."""
        painter.setFont(_ui_font(11))
        rect = QRectF(_PAD_X, self.height() - 26, self.width() - _PAD_X * 2, 20)
        painter.setPen(self._ink())
        painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text(self._notice_key))

    def _hit_at(self, pos: QPointF) -> tuple[str, str | int] | None:
        """Return the control under pos."""
        for rect, kind, payload in reversed(self._hits):
            if rect.contains(pos):
                return kind, payload
        return None

    def _settings_scrollbar(self) -> tuple[QRectF, QRectF, float] | None:
        """Return the track, the thumb, and how far the thumb can travel.

        Purpose:
            Place a visible bar on the right of the open settings panel.

        Parameters:
            None.

        Returns:
            tuple[QRectF, QRectF, float] | None: Track, thumb, and thumb
            travel in pixels. None when the list fits in the panel.

        Precondition:
            The window size already includes the settings panel.

        Postcondition:
            The returned rectangles stay inside the settings view.
        """
        limit = self._settings_max_scroll()
        if limit <= 0:
            return None
        view = self._settings_view_rect()
        track = QRectF(view.right() - _SCROLL_W - 4, view.top() + 8, 4, max(1, view.height() - 16))
        content = self._settings_content_height()
        thumb_h = max(16.0, track.height() * view.height() / max(content, 1))
        thumb_h = min(thumb_h, track.height())
        travel = max(1.0, track.height() - thumb_h)
        thumb_y = track.top() + (self._settings_scroll / limit) * travel
        thumb = QRectF(track.left() - 1, thumb_y, 6, thumb_h)
        return track, thumb, travel

    def _begin_scroll_drag(self, pos: QPointF) -> bool:
        """Start a thumb drag, or jump the list when the track is clicked.

        Purpose:
            Let the pointer move the settings list as well as the wheel.

        Parameters:
            pos (QPointF): Pointer position in widget coordinates.

        Returns:
            bool: True when the press landed on the scrollbar.

        Precondition:
            The settings panel is open.

        Postcondition:
            A thumb press stores the start point. A track press moves the list.
        """
        bar = self._settings_scrollbar()
        if bar is None:
            return False
        track, thumb, travel = bar
        if thumb.contains(pos):
            self._scroll_drag = (pos.y(), self._settings_scroll, travel)
            return True
        if not track.adjusted(-4, 0, 4, 0).contains(pos):
            return False
        limit = self._settings_max_scroll()
        ratio = (pos.y() - track.top() - thumb.height() / 2) / travel
        self._settings_scroll = max(0, min(limit, int(ratio * limit)))
        self._scroll_drag = (pos.y(), self._settings_scroll, travel)
        self.update()
        return True

    def _move_scroll_drag(self, y: float) -> None:
        """Move the settings list with the scrollbar thumb."""
        if self._scroll_drag is None:
            return
        origin_y, origin_scroll, travel = self._scroll_drag
        limit = self._settings_max_scroll()
        delta = (y - origin_y) / travel * limit
        self._settings_scroll = max(0, min(limit, int(origin_scroll + delta)))
        self.update()

    def _language_codes(self) -> list[str]:
        """Return language codes, English first, then by native name."""
        codes = [code for code in _TEXT if code != "en"]
        codes.sort(key=lambda code: _TEXT[code].get("language_value", code).casefold())
        if "en" in _TEXT:
            codes.insert(0, "en")
        return codes


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
