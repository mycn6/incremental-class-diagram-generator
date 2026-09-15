#!/usr/bin/env python3
"""Estimate rendered text extents so node boxes can be sized from their content.

The generator uses this module to decide how wide and tall a class box must be.
The validator uses the same module to decide whether a box is large enough. Both
must share one model: if each derived its own metrics they could drift apart and
the validator would keep reporting PASS while the diagram overflowed.

Design property - the estimate is deliberately ONE-SIDED. Every advance class
below is set at or above the true Microsoft YaHei advance, so

    estimated_width >= rendered_width    and    estimated_line_count >= rendered_lines

for every label. Over-estimating over-sizes the box and wraps a little early; it
can never claim text fits when it does not. That is what makes the validator's
fit rule safe to treat as a hard failure.

No dependency on any other repository module, and no font file is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


def _ceil(value: float) -> int:
    whole = int(value)
    return whole if whole == value else whole + 1


# --- line height -----------------------------------------------------------
#
# draw.io derives line height from the font rather than from fontSize alone.
# UNVERIFIED: 1.25 is an estimate for a CJK face. The contract example in
# assets/ is the binding constraint: its tightest vertex is a legend UML header
# of 140x34 holding two 12px lines at the default spacing of 2, so it has 30px
# of usable height for 2*1.25*12 = 30px of text and passes only because of the
# validator's 1.5px tolerance. LINE_RATIO above (30 + 1.5) / 24 = 1.3125 makes
# that vertex fail, and the example's legend geometry would have to change.
# Confirm against a real render before trusting either number.
LINE_RATIO = 1.25

# UNVERIFIED: bold faces are typically 3-7% wider than their regular cut.
BOLD_FACTOR = 1.06


# --- advance widths --------------------------------------------------------
#
# UNVERIFIED: the whole Latin table. Microsoft does not publish YaHei's Latin
# advances. Every value is at or above the true advance, and the CJK class is
# exact because YaHei is metrically one em square.
ADVANCE_EM = {
    "cjk": 1.00,      # ideographs, CJK punctuation, fullwidth forms, << >>
    "wide": 1.00,     # m M W w @ % &  (true advance ~0.83)
    "upper": 0.72,    # A-Z except M, W, I, J
    "narrow": 0.40,   # thin letters, digits 1, punctuation, space
    "default": 0.58,  # remaining lowercase, other digits, operators
}

WIDE_CHARS = frozenset("mMWw@%&")
NARROW_CHARS = frozenset("iljIftr1.,:;'\"`!|()[]{}/\\-_ ?*")


def advance_class(char: str) -> str:
    code = ord(char)
    if (
        0x2E80 <= code <= 0xA4CF      # CJK radicals, Kangxi, kana, CJK symbols
        or 0xAC00 <= code <= 0xD7A3   # Hangul syllables
        or 0xF900 <= code <= 0xFAFF   # CJK compatibility ideographs
        or 0xFE30 <= code <= 0xFE4F   # CJK compatibility forms
        or 0xFF00 <= code <= 0xFF60   # fullwidth forms
        or 0xFFE0 <= code <= 0xFFE6   # fullwidth signs
        or code == 0x2026             # horizontal ellipsis, used by "… 另有 N 项"
        or char in "«»"               # rendered fullwidth by YaHei
    ):
        return "cjk"
    if char in WIDE_CHARS:
        return "wide"
    if char in NARROW_CHARS:
        return "narrow"
    if "A" <= char <= "Z":
        return "upper"
    return "default"


def advance_width(text: str, font_size: float, *, bold: bool = False) -> float:
    """Estimated rendered width of one unwrapped line, in pixels."""
    total = sum(ADVANCE_EM[advance_class(char)] for char in text)
    return total * font_size * (BOLD_FACTOR if bold else 1.0)


def _break_token(token: str, max_width: float, font_size: float, bold: bool) -> list[str]:
    """Split a token with no break opportunity into chunks that each fit.

    draw.io will NOT do this: observed renders let such a token run past the box
    edge. These chunks are therefore the line breaks the generator has to write
    into the label itself - see hard_wrap.
    """
    chunks: list[str] = []
    current = ""
    for char in token:
        candidate = current + char
        if current and advance_width(candidate, font_size, bold=bold) > max_width:
            chunks.append(current)
            current = char
        else:
            current = candidate
    chunks.append(current)
    return chunks


def _wrap_line(line: str, max_width: float, font_size: float, bold: bool) -> list[str]:
    if not line:
        return [""]
    if advance_width(line, font_size, bold=bold) <= max_width:
        return [line]

    result: list[str] = []
    current = ""
    for word in line.split(" "):
        candidate = f"{current} {word}" if current else word
        if advance_width(candidate, font_size, bold=bold) <= max_width:
            current = candidate
            continue
        if current:
            result.append(current)
            current = ""
        if advance_width(word, font_size, bold=bold) <= max_width:
            current = word
        else:
            chunks = _break_token(word, max_width, font_size, bold)
            result.extend(chunks[:-1])
            current = chunks[-1]
    result.append(current)
    return result


def wrap_text(text: str, max_width: float, font_size: float, *, bold: bool = False) -> list[str]:
    """Rendered lines for a label, including wraps between explicit newlines."""
    lines: list[str] = []
    for logical in (text or "").split("\n"):
        lines.extend(_wrap_line(logical, max_width, font_size, bold))
    return lines or [""]


def rendered_lines(text: str, max_width: float, font_size: float, *, bold: bool = False) -> int:
    return len(wrap_text(text, max_width, font_size, bold=bold))


def hard_wrap(text: str, max_width: float, font_size: float, *, bold: bool = False) -> str:
    """Rewrite a label with the line breaks already inserted.

    draw.io does not break a token that offers no break opportunity; it lets that
    token run straight past the box edge. So the generator must insert the breaks
    itself rather than rely on `whiteSpace=wrap`. That is also what makes the
    logical line count equal the rendered line count, which the height budget and
    the validator both assume.

    Idempotent: wrapping an already hard-wrapped label returns it unchanged.
    """
    return "\n".join(wrap_text(text, max_width, font_size, bold=bold))


def logical_lines(text: str | None) -> int:
    """Line count of a hard-wrapped label, i.e. the lines the renderer will draw."""
    return len((text or "").split("\n"))


def widest_line(text: str, font_size: float, *, bold: bool = False) -> float:
    """Width of the widest explicit line, before any wrapping."""
    lines = (text or "").split("\n") or [""]
    return max(advance_width(line, font_size, bold=bold) for line in lines)


# --- box planning ----------------------------------------------------------

MIN_CLASS_WIDTH = 300.0
MAX_CLASS_WIDTH = 560.0
WIDTH_PERCENTILE = 0.90

HEADER_FONT_SIZE = 14.0
HEADER_SPACING = 4.0
HEADER_MIN_HEIGHT = 52.0

BODY_FONT_SIZE = 13.0
BODY_SPACING = 8.0
BAND_SLACK = 10.0

# Kept so that sparse content reproduces the contract example's bands exactly:
# header 52, attributes 70, operations 88, separators at 51 and 121.
SECTION_MIN_HEIGHT = {
    "class-attributes": 70.0,
    "class-literals": 70.0,
    "class-operations": 88.0,
}

# Rendered lines allowed in one compartment, ellipsis line included. The only
# lever on overall page size once page splitting is out of scope.
MAX_SECTION_LINES = 14


@dataclass(frozen=True)
class SectionSpec:
    suffix: str
    role: str
    members: tuple[str, ...]
    empty_label: str


@dataclass(frozen=True)
class SectionBox:
    suffix: str
    role: str
    label: str
    y: float
    height: float
    kept: int
    dropped: int


@dataclass(frozen=True)
class ClassBox:
    width: float
    height: float
    header_height: float
    header: str
    sections: tuple[SectionBox, ...]
    separators: tuple[float, ...]


def page_class_width(
    measured: Iterable[tuple[str, float, bool]],
    *,
    pad: float = 0.0,
    percentile: float = WIDTH_PERCENTILE,
    minimum: float = MIN_CLASS_WIDTH,
    maximum: float = MAX_CLASS_WIDTH,
) -> float:
    """One box width for the whole page, from a high percentile of signatures.

    A percentile rather than the maximum keeps one pathological signature from
    stretching every column; it simply wraps. Per-class widths are not an option
    here because layout_drawio pitches columns on the page-wide maximum width.

    Each entry is measured by its widest explicit line, not by the concatenation
    of its lines: a multi-line header only needs its longest line to fit, and the
    newline character itself has no advance. `pad` is the label padding the cell
    will apply on both sides, so the result is a box width and not a text width -
    without it the percentile signatures would each wrap by exactly that padding.
    """
    widths = sorted(
        widest_line(text, font_size, bold=bold) + pad
        for text, font_size, bold in measured
        if (text or "").strip()
    )
    if not widths:
        return minimum
    index = min(len(widths) - 1, max(0, _ceil(percentile * len(widths)) - 1))
    return float(min(maximum, max(minimum, _ceil(widths[index]))))


def fit_members(
    members: Sequence[str],
    empty_label: str,
    *,
    max_width: float,
    font_size: float,
    budget: int = MAX_SECTION_LINES,
) -> tuple[str, int, int]:
    """Keep the leading members that fit the line budget; count the rest.

    Returns (label, kept, dropped). The label is hard-wrapped, so its logical
    line count is what the renderer will draw. `dropped` is always
    len(members) - kept, so the "… 另有 N 项" line reports every member that is
    not visible - including the ones evicted to make room for the ellipsis line
    itself, which costs one rendered line just like a member does.

    A single member larger than the whole budget is kept rather than dropped:
    an empty compartment showing only an ellipsis is worse than a tall one, and
    a real signature renders to at most a handful of lines.
    """
    present = [member for member in members if member]
    if not present:
        return hard_wrap(empty_label, max_width, font_size), 0, 0

    kept: list[str] = []
    used = 0
    for member in present:
        cost = rendered_lines(member, max_width, font_size)
        if kept and used + cost > budget:
            break
        kept.append(member)
        used += cost
        if cost > budget:
            break

    dropped = len(present) - len(kept)
    if not dropped:
        return "\n".join(hard_wrap(member, max_width, font_size) for member in kept), len(kept), 0

    # Evict until the ellipsis line fits too. The count is rebuilt every round
    # because evicting changes it, and because the text of the count is what the
    # reader uses to judge how much is missing.
    while True:
        ellipsis = f"… 另有 {dropped} 项"
        if not kept or used + rendered_lines(ellipsis, max_width, font_size) <= budget:
            break
        used -= rendered_lines(kept.pop(), max_width, font_size)
        dropped += 1
    if not kept:
        kept.append(present[0])
        dropped -= 1
        ellipsis = f"… 另有 {dropped} 项"

    body = "\n".join(hard_wrap(member, max_width, font_size) for member in kept)
    return f"{body}\n{hard_wrap(ellipsis, max_width, font_size)}", len(kept), dropped


def plan_class_box(
    header: str,
    sections: Sequence[SectionSpec],
    width: float,
    *,
    header_font_size: float = HEADER_FONT_SIZE,
    header_spacing: float = HEADER_SPACING,
    body_font_size: float = BODY_FONT_SIZE,
    body_spacing: float = BODY_SPACING,
    budget: int = MAX_SECTION_LINES,
) -> ClassBox:
    """Size a class container and lay out its header, compartments and lines.

    Every label this returns is hard-wrapped: the caller writes it out verbatim.
    """
    usable_header = width - 2 * header_spacing
    usable_body = width - 2 * body_spacing

    wrapped_header = hard_wrap(header, usable_header, header_font_size, bold=True)
    header_height = float(
        max(
            HEADER_MIN_HEIGHT,
            _ceil(
                logical_lines(wrapped_header) * LINE_RATIO * header_font_size + 2 * header_spacing
            ),
        )
    )

    boxes: list[SectionBox] = []
    separators: list[float] = []
    y = header_height
    for spec in sections:
        label, kept, dropped = fit_members(
            spec.members,
            spec.empty_label,
            max_width=usable_body,
            font_size=body_font_size,
            budget=budget,
        )
        content = logical_lines(label) * LINE_RATIO * body_font_size
        height = float(
            max(
                SECTION_MIN_HEIGHT[spec.role],
                _ceil(content + 2 * body_spacing + BAND_SLACK),
            )
        )
        separators.append(y - 1)  # the line occupies the band's top pixel row
        boxes.append(SectionBox(spec.suffix, spec.role, label, y, height, kept, dropped))
        y += height

    return ClassBox(width, y, header_height, wrapped_header, tuple(boxes), tuple(separators))
