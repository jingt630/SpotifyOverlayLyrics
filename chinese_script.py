"""Offline simplified and traditional copies of lyric text.

OpenCC runs when a lyric file is first stored. Later reads use the saved
copies. Song lyrics stay in their own files. This module does not change
the overlay's interface language.
"""

from __future__ import annotations

import json
from pathlib import Path

from opencc import OpenCC

from app_paths import resource_dir, user_data_dir

_SCRIPT_DIR = user_data_dir() / ".spotify" / "lyric_script"
_KANJI_PATH = resource_dir() / "chinese_script_data" / "japanese_kanji.txt"
_MODES = ("original", "simplified", "traditional")
_s2t: OpenCC | None = None
_t2s: OpenCC | None = None
_lrclib_cache: dict[int, list[dict[str, str]]] = {}
_japanese_kanji: set[str] | None = None


def _to_traditional() -> OpenCC:
    """Return the simplified-to-traditional converter, creating it once."""
    global _s2t
    if _s2t is None:
        _s2t = OpenCC("s2t")
    return _s2t


def _to_simplified() -> OpenCC:
    """Return the traditional-to-simplified converter, creating it once."""
    global _t2s
    if _t2s is None:
        _t2s = OpenCC("t2s")
    return _t2s


def normalize_mode(mode: str) -> str:
    """Return original, simplified, or traditional."""
    if mode in _MODES:
        return mode
    return "original"


def both_scripts(text: str) -> tuple[str, str]:
    """Return the simplified copy and the traditional copy of text.

    Purpose:
        Convert Chinese with the local OpenCC dictionaries. Hiragana,
        katakana, hangul, and kanji that belong to a Japanese word stay
        as written. A run of characters that also exist in Japanese is
        left alone unless the same line has a Chinese character outside
        that set.

    Parameters:
        text (str): One lyric line or one timed word.

    Returns:
        tuple[str, str]: Simplified text, then traditional text.

    Precondition:
        text is a string. OpenCC dictionaries are installed.

    Postcondition:
        text is unchanged. Characters that are not converted are copied
        into both results.
    """
    return _convert(text, _to_simplified()), _convert(text, _to_traditional())


def stamp_lines(lines: list[dict]) -> bool:
    """Add text_s and text_t only where the text contains Chinese.

    Purpose:
        Keep both scripts beside Chinese lyric text. English, Japanese, and
        other text omit those fields.

    Parameters:
        lines (list[dict]): Timed lines. Each line may contain a words list.

    Returns:
        bool: True when at least one field was added.

    Precondition:
        lines contains dicts.

    Postcondition:
        A line or word that is not Chinese does not gain text_s or text_t.
        Timing fields are unchanged.
    """
    changed = False
    for line in lines:
        if not isinstance(line, dict):
            continue
        if _stamp_node(line):
            changed = True
        words = line.get("words")
        if not isinstance(words, list):
            continue
        source = str(line.get("text") or "")
        tokens = [word for word in words if isinstance(word, dict)]
        joined = "".join(str(word.get("text") or "") for word in tokens)
        simplified = str(line.get("text_s") or "")
        traditional = str(line.get("text_t") or "")
        flags = _convert_flags(source)
        if (
            joined == source
            and "text_s" in line
            and "text_t" in line
            and len(simplified) == len(source)
            and len(traditional) == len(source)
            and len(flags) == len(source)
        ):
            pos = 0
            for word in tokens:
                token = str(word.get("text") or "")
                end = pos + len(token)
                if any(flags[pos:end]) and ("text_s" not in word or "text_t" not in word):
                    word["text_s"] = simplified[pos:end]
                    word["text_t"] = traditional[pos:end]
                    changed = True
                pos = end
            continue
        masks = _word_masks(source, words)
        for word, mask in zip(words, masks):
            if isinstance(word, dict) and _stamp_node(word, mask):
                changed = True
    return changed


def shown_text(node: dict, mode: str) -> str:
    """Return the lyric string for the selected Chinese script.

    Purpose:
        Read a stored copy. Original mode returns the source text.

    Parameters:
        node (dict): A line or word with text, and text_s / text_t when stored.
        mode (str): original, simplified, or traditional.

    Returns:
        str: The string to draw.

    Precondition:
        node may omit the converted fields.

    Postcondition:
        node is unchanged.
    """
    mode = normalize_mode(mode)
    if mode == "simplified":
        return str(node.get("text_s") or node.get("text") or "")
    if mode == "traditional":
        return str(node.get("text_t") or node.get("text") or "")
    return str(node.get("text") or "")


def lrclib_lines(record_id: int, texts: tuple[str, ...], mode: str) -> tuple[str, ...] | None:
    """Return stored script copies for one LRCLIB record, writing them on first use.

    Purpose:
        Save simplified and traditional line text under .spotify/lyric_script
        so the next play does not convert again.

    Parameters:
        record_id (int): LRCLIB record id. This is the file name.
        texts (tuple[str, ...]): Original line text, in lyric order.
        mode (str): original, simplified, or traditional.

    Returns:
        tuple[str, ...] | None: One string per line for mode. None when mode
        is original, so the caller keeps the source lines.

    Precondition:
        texts is the full line list for record_id.

    Postcondition:
        .spotify/lyric_script/<record_id>.json exists and matches texts.
        A file whose original lines still match is not rewritten.
    """
    if normalize_mode(mode) == "original":
        return None
    rows = _load_lrclib(record_id, texts)
    key = "text_s" if mode == "simplified" else "text_t"
    return tuple(str(row.get(key) or row.get("text") or "") for row in rows)


def _stamp_node(node: dict, mask: list[bool] | None = None) -> bool:
    """Fill text_s and text_t when this text contains Chinese and either field is missing."""
    text = str(node.get("text") or "")
    if mask is not None and len(mask) == len(text):
        chinese = any(mask)
    else:
        chinese = any(_convert_flags(text))
    if not chinese:
        return False
    if "text_s" in node and "text_t" in node:
        return False
    if mask is not None and len(mask) == len(text):
        node["text_s"] = _convert_masked(text, mask, _to_simplified())
        node["text_t"] = _convert_masked(text, mask, _to_traditional())
        return True
    simplified, traditional = both_scripts(text)
    node["text_s"] = simplified
    node["text_t"] = traditional
    return True


def _japanese_set() -> set[str]:
    """Return joyo and jinmeiyo kanji. Those characters are not converted on their own."""
    global _japanese_kanji
    if _japanese_kanji is None:
        raw = _KANJI_PATH.read_text(encoding="utf-8") if _KANJI_PATH.exists() else ""
        _japanese_kanji = set(raw)
    return _japanese_kanji


def _is_kana(char: str) -> bool:
    """Return whether char is hiragana, katakana, or a Japanese prolonged mark."""
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF
        or 0x31F0 <= code <= 0x31FF
        or 0xFF66 <= code <= 0xFF9D
        or char in "ー"
    )


def _is_ideograph(char: str) -> bool:
    """Return whether char is a CJK ideograph."""
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def _atoms(text: str) -> list[tuple[str, str]]:
    """Split text into kana, ideograph, space, and other runs."""
    atoms: list[tuple[str, str]] = []
    for char in text:
        if _is_kana(char):
            kind = "kana"
        elif _is_ideograph(char):
            kind = "ideo"
        elif char.isspace():
            kind = "space"
        else:
            kind = "other"
        if atoms and atoms[-1][0] == kind:
            atoms[-1] = (kind, atoms[-1][1] + char)
        else:
            atoms.append((kind, char))
    return atoms


def _convert_flags(text: str) -> list[bool]:
    """Return one flag per character. True where a Chinese character may change.

    Purpose:
        Kanji touching hiragana or katakana stay fixed. An ideograph run
        changes only when it contains a character outside the Japanese
        kanji set, and then the whole run changes so a phrase such as
        后面 can become 後面.
    """
    atoms = _atoms(text)
    japanese_ideo: set[int] = set()
    for index, (kind, _chunk) in enumerate(atoms):
        if kind != "ideo":
            continue
        for step in (-1, 1):
            cursor = index + step
            while 0 <= cursor < len(atoms) and atoms[cursor][0] == "space":
                cursor += step
            if 0 <= cursor < len(atoms) and atoms[cursor][0] == "kana":
                japanese_ideo.add(index)
                break
    has_chinese = False
    for index, (kind, chunk) in enumerate(atoms):
        if kind == "ideo" and index not in japanese_ideo:
            if any(char not in _japanese_set() for char in chunk):
                has_chinese = True
                break
    flags: list[bool] = []
    for index, (kind, chunk) in enumerate(atoms):
        convert = kind == "ideo" and index not in japanese_ideo and has_chinese
        flags.extend([convert] * len(chunk))
    return flags


def _convert(text: str, converter: OpenCC) -> str:
    """Convert Chinese runs and copy every other character."""
    return _convert_masked(text, _convert_flags(text), converter)


def _convert_masked(text: str, flags: list[bool], converter: OpenCC) -> str:
    """Convert the flagged characters and copy the rest."""
    if len(flags) != len(text) or not any(flags):
        return text
    parts: list[str] = []
    start = 0
    while start < len(text):
        convert = flags[start]
        end = start + 1
        while end < len(text) and flags[end] == convert:
            end += 1
        chunk = text[start:end]
        parts.append(converter.convert(chunk) if convert else chunk)
        start = end
    return "".join(parts)


def _word_masks(line_text: str, words: list) -> list[list[bool]]:
    """Return convert-flags for each word, aligned onto the line when possible."""
    flags = _convert_flags(line_text)
    masks: list[list[bool]] = []
    pos = 0
    for word in words:
        if not isinstance(word, dict):
            masks.append([])
            continue
        token = str(word.get("text") or "")
        at = line_text.find(token, pos) if token else -1
        if at < 0:
            masks.append(_convert_flags(token))
            continue
        masks.append(flags[at : at + len(token)])
        pos = at + len(token)
    return masks


def _load_lrclib(record_id: int, texts: tuple[str, ...]) -> list[dict[str, str]]:
    """Return the stored rows for this record, rebuilding them when the lyrics differ."""
    cached = _lrclib_cache.get(record_id)
    if cached is not None and _rows_match(cached, texts):
        return cached
    path = _SCRIPT_DIR / f"{record_id}.json"
    rows: list[dict[str, str]] | None = None
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        loaded = raw.get("lines") if isinstance(raw, dict) else None
        if isinstance(loaded, list) and _rows_match(loaded, texts):
            rows = loaded
    if rows is None:
        rows = []
        for text in texts:
            row = {"text": text}
            if any(_convert_flags(text)):
                simplified, traditional = both_scripts(text)
                row["text_s"] = simplified
                row["text_t"] = traditional
            rows.append(row)
        import lyric_catalog

        if lyric_catalog.store_locally():
            _SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"record_id": record_id, "lines": rows}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    _lrclib_cache[record_id] = rows
    return rows


def _rows_match(rows: list, texts: tuple[str, ...]) -> bool:
    """Return whether every stored original line still equals the current lyric."""
    if len(rows) != len(texts):
        return False
    for row, text in zip(rows, texts):
        if not isinstance(row, dict) or row.get("text") != text:
            return False
        if any(_convert_flags(text)):
            if "text_s" not in row or "text_t" not in row:
                return False
        elif "text_s" in row or "text_t" in row:
            return False
    return True
