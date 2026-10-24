"""Lossless, page-bound LTR reading order and typed schedule/reference blocks.

Only whitespace is reflowed. Every visible PDF glyph has an index; the partition
covers every non-whitespace character exactly once. This never edits stored clauses.
"""

import hashlib
import math
import re
from collections import Counter

LAYOUT_ENGINE = "pdf-ltr-typed-v1"
SCHEDULE = re.compile(r"^(?:(?:FIRST|SECOND|THIRD|FOURTH|FIFTH)\s+)?SCHEDULE\b")
REFERENCE = re.compile(r"^\d{1,3}\s*[-–]\s+")
ROW = re.compile(r"^\d+\.\s+.*(?:\$|€|£)\s*\d")


def is_english_page(raw):
    letters = [char for char in raw if char.isalpha()]
    return bool(letters) and sum(char.isascii() for char in letters) / len(letters) >= 0.99


def ltr_page(text_page):
    """Return glyph-indexed English-only page. Never reorder Arabic/mixed text."""
    raw = text_page.get_text_range()
    if not is_english_page(raw):
        raise ValueError("LTR repair requires an English-only page; mixed/Arabic text refused")
    lines = []
    for index in range(text_page.count_chars()):
        char = text_page.get_text_range(index, 1)
        if char in ("", "\r", "\n"):
            continue
        left, bottom, right, top = text_page.get_charbox(index, loose=True)
        centre = (bottom + top) / 2
        line = next((row for row in lines if abs(row[0] - centre) < 3), None)
        if line is None:
            line = [centre, []]
            lines.append(line)
        line[1].append((left, right, char, index))
    texts, positions, anchors, glyph_offsets = [], [], [], {}
    offset = 0
    for _, glyphs in sorted(lines, key=lambda row: -row[0]):
        glyphs.sort()
        text, end, indices, offsets = "", None, [], {}
        for left, right, char, index in glyphs:
            if end is not None and left - end > 2.5 and not char.isspace():
                text += " "
            offsets[index] = len(text)
            text += char
            end = right
            indices.append(index)
        trim = len(text) - len(text.lstrip())
        text = text.strip()
        if not text:
            continue
        anchors.append(dict(start=offset, end=offset + len(text), glyph_indices=indices))
        glyph_offsets.update(
            {
                str(i): offset + pos - trim
                for i, pos in offsets.items()
                if text_page.get_text_range(i, 1).strip()
            }
        )
        texts.append(text)
        positions.extend(indices)
        offset += len(text) + 1
    reconstructed = "\n".join(texts)
    expected = [i for i in range(text_page.count_chars()) if text_page.get_text_range(i, 1).strip()]
    visible_positions = [i for i in positions if text_page.get_text_range(i, 1).strip()]
    if Counter(expected) != Counter(visible_positions) or Counter(filter_nonwhite(raw)) != Counter(
        filter_nonwhite(reconstructed)
    ):
        raise ValueError("PDF glyph coverage differs; repair refused")
    return dict(
        raw_pdf_text=raw,
        text=reconstructed,
        lines=anchors,
        glyph_offsets=glyph_offsets,
        visible_glyph_count=len(expected),
        glyph_coverage_passed=True,
        engine=LAYOUT_ENGINE,
    )


def resolve_unmapped_hyphens(page, text_page, layout):
    """Decode only unmapped glyphs with an exact same-page hyphen ink match.

    Keep both raw readings. This is font decoding, never spelling/URL completion.
    At least two independently encoded '-' exemplars must have identical binary ink
    at 12 pixels/point. An unknown mark without that proof makes the repair fail closed.
    """
    unknown = [
        i
        for i in range(text_page.count_chars())
        if text_page.get_text_range(i, 1) in ("\ufffe", "\ufffd")
    ]
    if not unknown:
        return layout
    known = [i for i in range(text_page.count_chars()) if text_page.get_text_range(i, 1) == "-"]
    bitmap = page.render(scale=12)
    try:
        image = bitmap.to_pil().convert("L")

        def ink(index):
            left, bottom, right, top = text_page.get_charbox(index)
            box = (
                math.floor(left * 12) - 2,
                math.floor((page.get_height() - top) * 12) - 2,
                math.ceil(right * 12) + 2,
                math.ceil((page.get_height() - bottom) * 12) + 2,
            )
            cropped = image.crop(box).point(lambda value: 255 if value < 128 else 0)
            bounds = cropped.getbbox()
            if bounds is None:
                raise ValueError("unmapped glyph has no visible ink")
            cropped = cropped.crop(bounds)
            return cropped.size, cropped.tobytes()

        exemplars = [(index, ink(index)) for index in known]
        text, proofs = layout["text"], []
        for index in unknown:
            shape = ink(index)
            matches = [i for i, candidate in exemplars if candidate == shape]
            if len(matches) < 2:
                raise ValueError("unmapped PDF glyph has no exact same-page decoding proof")
            offset = layout["glyph_offsets"][str(index)]
            proofs.append(
                dict(
                    glyph_index=index,
                    offset=offset,
                    raw=text[offset],
                    decoded="-",
                    method="exact_same_page_binary_ink_12px_per_point",
                    exemplar_glyph_indices=matches,
                    ink_size=list(shape[0]),
                    ink_sha256=hashlib.sha256(shape[1]).hexdigest(),
                )
            )
            text = text[:offset] + "-" + text[offset + 1 :]
        return {
            **layout,
            "raw_layout_text": layout["text"],
            "text": text,
            "glyph_decodings": proofs,
        }
    finally:
        bitmap.close()


def filter_nonwhite(text):
    return "".join(char for char in text if not char.isspace())


def is_reference_page(text):
    return (
        sum(bool(REFERENCE.match(line)) for line in text.splitlines()) >= 3
        and text.count("https://") >= 3
    )


def _url_spans(text):
    """Keep raw wrapped URL bytes, including visual line breaks. Never invent a URL."""
    spans = []
    for match in re.finditer(r"https?://[^\s]*", text):
        if spans and match.start() < spans[-1][1]:
            continue
        end = match.end()
        while end < len(text):
            follow = re.match(r"\n([A-Za-z0-9%_./?=&:#+~;,@!$*()\-]+)(?=\s|$)", text[end:])
            if not follow:
                break
            token = follow.group(1)
            if not any(ch in token for ch in "/?=&%._:#"):
                break
            end += follow.end()
        spans.append((match.start(), end))
    return spans


def atomic_line_blocks(text):
    """Page-local confirmed tables and bibliography entries, never ordinary schedules."""
    lines = text.splitlines()
    starts = [
        i
        for i, line in enumerate(lines)
        if SCHEDULE.match(line) or REFERENCE.match(line) or line.startswith("Made this ")
    ]
    result = {}
    has_table = False
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        segment = lines[start:end]
        table = (
            SCHEDULE.match(lines[start])
            and "First column" in "\n".join(segment)
            and "Second column" in "\n".join(segment)
            and sum(bool(ROW.match(line)) for line in segment) >= 2
        )
        if table:
            result[start] = (end, "TABLE / " + lines[start])
            has_table = True
        elif REFERENCE.match(lines[start]) and is_reference_page(text):
            result[start] = (end, "REFERENCE / " + REFERENCE.match(lines[start]).group().strip())
        elif has_table and lines[start].startswith("Made this "):
            result[start] = (end, "SIGNATURE")
    return result


def partition_page(text):
    """Partition full page into narrative, literal table rows/IDs and raw URL fields.

    Literal data is NOT called a successful translation. Its only allowed rendering
    is byte-for-byte source copying. Language units remain subject to all normal gates.
    """
    lines = [(m.start(), m.end(), m.group()) for m in re.finditer(r"[^\n]+", text)]
    starts = [
        (index, start, value)
        for index, (start, _, value) in enumerate(lines)
        if SCHEDULE.match(value) or REFERENCE.match(value) or value.startswith("Made this ")
    ]
    blocks = []
    if starts and starts[0][1]:
        blocks.append(dict(kind="page_identifier", start=0, end=starts[0][1]))
    for number, (index, start, value) in enumerate(starts):
        end = starts[number + 1][1] if number + 1 < len(starts) else len(text)
        kind = (
            "reference"
            if REFERENCE.match(value)
            else "signature"
            if value.startswith("Made this ")
            else "table"
        )
        blocks.append(dict(kind=kind, start=start, end=end, first_line=index))
    if not blocks:
        raise ValueError("no supported table/reference/signature layout; repair refused")
    pieces = []
    for block_no, block in enumerate(blocks):
        start, end, kind = block["start"], block["end"], block["kind"]
        ranges = []
        if kind == "page_identifier":
            ranges.append((start, end, "page_identifier"))
        elif kind == "table":
            rows = [(a, b) for a, b, value in lines if start <= a < end and ROW.match(value)]
            if (
                len(rows) < 2
                or "First column" not in text[start:end]
                or "Second column" not in text[start:end]
            ):
                raise ValueError("schedule lacks an intact two-column table; repair refused")
            for a, b in rows:
                cursor = a
                for words in re.finditer(r"[A-Za-z]+(?:\s+[A-Za-z]+)*", text[a:b]):
                    x, y = a + words.start(), a + words.end()
                    if x > cursor:
                        ranges.append((cursor, x, "table_numeric_cell"))
                    ranges.append((x, y, "cell_text"))
                    cursor = y
                if cursor < b:
                    ranges.append((cursor, b, "table_numeric_cell"))
        elif kind == "signature":
            for a, b, value in lines:
                if start <= a < end and (
                    value.startswith("[")
                    or (value == value.upper() and value.replace(" ", "").isalpha())
                ):
                    ranges.append(
                        (a, b, "document_identifier" if value.startswith("[") else "signatory_name")
                    )
        else:
            prefix = REFERENCE.match(text[start:end])
            ranges.append((start, start + prefix.end(), "reference_number"))
            ranges.extend(
                (start + a, start + b, "source_url") for a, b in _url_spans(text[start:end])
            )
        position = start
        for a, b, literal_kind in sorted(ranges):
            if a < position or b > end:
                raise ValueError("overlapping source fields; repair refused")
            if a > position:
                pieces.append(dict(block=block_no, kind="text", start=position, end=a))
            pieces.append(dict(block=block_no, kind=literal_kind, start=a, end=b))
            position = b
        if position < end:
            pieces.append(dict(block=block_no, kind="text", start=position, end=end))
    for piece in pieces:
        piece["source_text"] = text[piece["start"] : piece["end"]]
    if "".join(piece["source_text"] for piece in pieces) != text:
        raise ValueError("source partition omitted or duplicated content")
    units = []
    for block_no, block in enumerate(blocks):
        header = [
            piece
            for piece in pieces
            if piece["block"] == block_no
            and piece["kind"] == "text"
            and piece["source_text"].strip()
        ]
        cells = [
            [piece]
            for piece in pieces
            if piece["block"] == block_no and piece["kind"] == "cell_text"
        ]
        for spans in ([header] if header else []) + cells:
            units.append(
                dict(
                    block=block_no,
                    kind=block["kind"],
                    language="en",
                    source_spans=[dict(start=piece["start"], end=piece["end"]) for piece in spans],
                    source_text="\n".join(piece["source_text"] for piece in spans),
                )
            )
    return dict(
        blocks=blocks,
        pieces=pieces,
        translation_units=units,
        literal_fields=[p for p in pieces if p["kind"] not in ("text", "cell_text")],
    )
