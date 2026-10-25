"""Country-independent sentence IDs backed by exact offsets in the unchanged source."""

import hashlib
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.knowledge.numeric_integrity import separate_explicit_dates

SELECTION_VERSION = "legal-list-coverage-v8"
_ARTICLE = re.compile(
    r"(?m)^[ \t\u200e\u200f]*(?:(?:Article\.?|Section\.?|Regulation\.?|Rule\.?|Art\.)\s+"
    r"[0-9A-Za-z]+(?:\.[0-9]+)*\s*[.:—–]|"
    r"(?:الفصل|ال[ \t]*م[ \t]*ا[ \t]*د[ \t]*ة|مادة)\s+[0-9٠-٩]+\s*[.:：]|"
    r"第[一二三四五六七八九十百千0-9]+条)",
    re.IGNORECASE,
)
_LIST_LINE = re.compile(r"(?m)^[ \t]*(?:[-–—•]|[0-9٠-٩]+\s*[-–]|\([a-z0-9]+\))\s*")
# Semicolons join legal lists to their chapeau; abbreviations must not split Art. / Sec.
_BOUNDARY = re.compile(r"(?<=[.!?])(?:[ \t]+|\r?\n)|(?<=[。！？؟])\s*|\n[ \t]*\n")
_ABBREVIATIONS = {
    "art",
    "arts",
    "article",
    "section",
    "regulation",
    "rule",
    "sec",
    "secs",
    "no",
    "nos",
    "mr",
    "mrs",
    "dr",
}
_DEFINITION = re.compile(
    r'(?m)^[ \t]*[“"«][^\n”"»]{1,120}[”"»]\s+(?:means|includes|refers to)\b',
    re.IGNORECASE,
)
_CHILD = re.compile(r"^(?:[-–—•●▪◦*\uf0b7\uf076]|\(?[\dA-Za-z]+[).、]|[\d]+\s*[-–])")
_QUALIFIER = re.compile(
    r"^(?:except\b|unless\b|provided\b|however\b|subject to\b|sauf\b|toutefois\b|"
    r"néanmoins\b|par dérogation\b|لكن|إلا|غير أن|ولكن|但是|但|除外|除非)",
    re.IGNORECASE,
)


class SourceBoundaryError(ValueError):
    """The selector must choose separate provisions, not repair source text."""


def provision_boundaries(text, heading=""):
    markers = [m.start() for m in _ARTICLE.finditer(text)]
    if markers and heading and " ".join(text[: markers[0]].split()) == " ".join(heading.split()):
        markers = markers[1:]
    return markers


def sentence_catalog(text: str) -> list[dict]:
    boundaries = []
    for m in _BOUNDARY.finditer(text):
        tail = re.search(r"([A-Za-z]+)\.$", text[: m.start()])
        if tail and tail.group(1).lower() in _ABBREVIATIONS:
            continue
        boundaries.append(m.end())
    offsets = [
        0,
        *boundaries,
        *(m.start() for m in _DEFINITION.finditer(text)),
        *(m.start() for m in _ARTICLE.finditer(text)),
        len(text),
    ]
    offsets = sorted(set(offsets))
    # Keep a chapeau's ordered list atomic for selection. Coverage verification
    # still checks every list item separately; the six-anchor Schema stays fixed.
    markers = list(_LIST_LINE.finditer(text))
    ordinal = re.compile(r"^[ \t]*([0-9٠-٩]+)\s*[-–]")
    articles = [m.start() for m in _ARTICLE.finditer(text)]
    atomic = []
    for n, marker in enumerate(markers):
        if not text[: marker.start()].rstrip().endswith((":", "：")):
            continue
        first = ordinal.match(marker.group())
        last = n
        for j in range(n + 1, len(markers)):
            previous, current = (
                ordinal.match(markers[j - 1].group()),
                ordinal.match(markers[j].group()),
            )
            if any(marker.start() < p < markers[j].start() for p in articles):
                break
            if re.search(r"\n[ \t]*\n", text[markers[j - 1].end() : markers[j].start()]):
                break
            if first and (not previous or not current or int(current[1]) != int(previous[1]) + 1):
                break
            if not first and current:
                break
            if text[markers[j - 1].end() : markers[j].start()].rstrip().endswith((":", "：")):
                break
            last = j
        if last > n:
            end = next(p for p in offsets if p >= markers[last].end())
            if not any(marker.start() < p < end for p in articles):
                atomic.append((marker.start(), end))
    offsets = [p for p in offsets if not any(start < p < end for start, end in atomic)]
    spans = []
    for start, end in zip(offsets, offsets[1:], strict=False):
        if not text[start:end].strip():
            if spans:
                spans[-1]["end"] = end
                spans[-1]["text"] = text[spans[-1]["start"] : end]
            continue
        spans.append(
            {"sentence_id": len(spans) + 1, "start": start, "end": end, "text": text[start:end]}
        )
    return spans


def select_quote(text: str, sentence_ids: list[int]) -> dict:
    catalog = sentence_catalog(text)
    if (
        not sentence_ids
        or any(type(i) is not int for i in sentence_ids)
        or sentence_ids != list(range(sentence_ids[0], sentence_ids[-1] + 1))
        or sentence_ids[0] < 1
        or sentence_ids[-1] > len(catalog)
    ):
        raise ValueError("sentence_id 无效、重复或不连续；必须选择所给编号")
    start, end = catalog[sentence_ids[0] - 1]["start"], catalog[sentence_ids[-1] - 1]["end"]
    quote = text[start:end]
    if len(quote.strip()) < 15:
        raise ValueError("所选原文不足15字符，请选择相邻完整句，不能补字")
    return {
        "quote": quote,
        "quote_start": start,
        "quote_end": end,
        "source_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "selection_version": SELECTION_VERSION,
        "sentence_ids": sentence_ids,
    }


def select_legal_quote(text: str, sentence_ids: list[int], *, heading: str = "") -> dict:
    """Close list references/provisos inside this unchanged clause, never across source IDs."""
    selected = select_quote(text, sentence_ids)
    markers = provision_boundaries(text, heading)
    # A separately parsed title followed by its own number belongs to one provision.
    # Only exact metadata correspondence may exempt the first marker; a preceding
    # normative paragraph or another heading is still an independent boundary.
    if any(selected["quote_start"] < p < selected["quote_end"] for p in markers):
        raise SourceBoundaryError(
            "选证跨越独立条文标题；请仅选择同一条文，或分为两项独立选证，不能混入前条尾段"
        )
    catalog = sentence_catalog(text)
    first, last = sentence_ids[0] - 1, sentence_ids[-1] - 1

    def prefix(value):
        # Directional formatting is invisible syntax, not a new source character.
        # Only inspect through it; actual quote offsets/text stay untouched.
        return "".join(c for c in value if unicodedata.category(c) != "Cf").lstrip()

    lead = prefix(catalog[first]["text"])
    if first and _CHILD.match(lead) and not _ARTICLE.match(lead):
        first -= 1
        while first and _CHILD.match(prefix(catalog[first]["text"])):
            first -= 1  # nearest list chapeau; do not pull in unrelated earlier definitions
    if first and _QUALIFIER.match(lead):
        first -= 1
    while last + 1 < len(catalog) and _QUALIFIER.match(prefix(catalog[last + 1]["text"])):
        last += 1
    expanded = select_quote(text, list(range(first + 1, last + 2)))
    if any(expanded["quote_start"] < p < expanded["quote_end"] for p in markers):
        raise SourceBoundaryError("列表或例外上下文跨越独立条文；请重新选择完整依据")
    expanded["selected_sentence_ids"] = sentence_ids
    expanded["anchor_quote_start"] = selected["quote_start"]
    expanded["anchor_quote_end"] = selected["quote_end"]
    return expanded


def coverage_units(quote):
    """All source units, including list alternatives. No source bytes are rewritten."""
    bounds = sorted(
        {
            0,
            len(quote),
            *(s["start"] for s in sentence_catalog(quote)),
            *(m.start() for m in _LIST_LINE.finditer(quote)),
        }
    )
    return [
        {"unit_id": i + 1, "start": a, "end": b, "text": quote[a:b]}
        for i, (a, b) in enumerate(zip(bounds, bounds[1:], strict=False))
        if quote[a:b].strip()
    ]


class CoveredUnit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    unit_id: int
    meaning: str = Field(min_length=1, max_length=2000)


class CoveredClaim(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    units: list[CoveredUnit] = Field(max_length=80)
    risk_level: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"
    gap: str = Field(default="", max_length=1500)

    @model_validator(mode="after")
    def exactly_units_or_gap(self):
        if bool(self.units) == bool(self.gap.strip()):
            raise ValueError("必须且只能逐段完整转述或说明证据缺口")
        return self


class SourceSupport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    supported: Literal["yes", "partial", "no"]
    reason: str = Field(min_length=1, max_length=1500)


def assemble_covered_claim(claim, units):
    if claim.gap:
        return ""
    if [u.unit_id for u in claim.units] != [u["unit_id"] for u in units]:
        raise ValueError("原文单元遗漏、重复、乱序或新增；必须逐个覆盖，不能跳过例外或列表项")
    for source, rendering in zip(units, claim.units, strict=True):
        source_numbers, source_dates, _ = separate_explicit_dates(source["text"])
        target_numbers, target_dates, _ = separate_explicit_dates(rendering.meaning)
        if source_numbers - target_numbers or source_dates - target_dates:
            raise ValueError(
                f"原文单元{source['unit_id']}遗漏数值或日期："
                f"{sorted(source_numbers - target_numbers)} {sorted(source_dates - target_dates)}；"
                "完整保留原文写法，不猜测缺损的范围连接词"
            )
    return "\n".join(u.meaning.strip() for u in claim.units)


class GroundedClaim(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    obligation: str = Field(default="", max_length=2000)
    risk_level: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"
    gap: str = Field(default="", max_length=1500)

    @model_validator(mode="after")
    def exactly_rule_or_gap(self):
        if bool(self.obligation.strip()) == bool(self.gap.strip()):
            raise ValueError("必须且只能返回一条完整有依据的规则或明确证据缺口")
        return self


class FindingSelection(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    clause_id: str = Field(min_length=1, max_length=64)
    sentence_ids: list[int] = Field(min_length=1, max_length=6)
    fact_key: str = Field(min_length=1, max_length=128)
    obligation: str = Field(min_length=1, max_length=2000)
    risk_level: Literal["HIGH", "MEDIUM", "LOW"]


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    summary: str = ""
    findings: list[FindingSelection] = Field(max_length=10)
    recommendations: list[str] = Field(default_factory=list, max_length=6)
    gaps: str = ""

    @model_validator(mode="after")
    def require_findings_or_explicit_gap(self):
        if not self.findings and not self.gaps.strip():
            raise ValueError("没有结论时必须说明缺少哪类法源，不能返回空成功")
        return self
