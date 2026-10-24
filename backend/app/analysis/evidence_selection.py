"""Country-independent sentence IDs backed by exact offsets in the unchanged source."""

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SELECTION_VERSION = "source-sentences-v1"
_BOUNDARY = re.compile(r"(?<=[.!?。！？؟؛;])(?:[ \t]+|\r?\n)|\n[ \t]*\n")


def sentence_catalog(text: str) -> list[dict]:
    offsets = [0, *(m.end() for m in _BOUNDARY.finditer(text)), len(text)]
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
