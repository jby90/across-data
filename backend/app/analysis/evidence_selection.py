"""Country-independent sentence IDs backed by exact offsets in the unchanged source."""

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SELECTION_VERSION = "legal-units-v2"
# Semicolons join legal lists to their chapeau; abbreviations must not split Art. / Sec.
_BOUNDARY = re.compile(r"(?<=[.!?])(?:[ \t]+|\r?\n)|(?<=[。！？؟])\s*|\n[ \t]*\n")
_ABBREVIATIONS = {"art", "arts", "sec", "secs", "no", "nos", "mr", "mrs", "dr"}
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


def sentence_catalog(text: str) -> list[dict]:
    boundaries = []
    for m in _BOUNDARY.finditer(text):
        tail = re.search(r"([A-Za-z]+)\.$", text[: m.start()])
        if tail and tail.group(1).lower() in _ABBREVIATIONS:
            continue
        boundaries.append(m.end())
    offsets = [0, *boundaries, *(m.start() for m in _DEFINITION.finditer(text)), len(text)]
    offsets = sorted(set(offsets))
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


def select_legal_quote(text: str, sentence_ids: list[int]) -> dict:
    """Close list references/provisos inside this unchanged clause, never across source IDs."""
    selected = select_quote(text, sentence_ids)
    catalog = sentence_catalog(text)
    first, last = sentence_ids[0] - 1, sentence_ids[-1] - 1
    lead = catalog[first]["text"].lstrip()
    if first and _CHILD.match(lead):
        first -= 1
        while first and _CHILD.match(catalog[first]["text"].lstrip()):
            first -= 1  # nearest list chapeau; do not pull in unrelated earlier definitions
    if first and _QUALIFIER.match(lead):
        first -= 1
    while last + 1 < len(catalog) and _QUALIFIER.match(catalog[last + 1]["text"].lstrip()):
        last += 1
    expanded = select_quote(text, list(range(first + 1, last + 2)))
    expanded["selected_sentence_ids"] = sentence_ids
    expanded["anchor_quote_start"] = selected["quote_start"]
    expanded["anchor_quote_end"] = selected["quote_end"]
    return expanded


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
