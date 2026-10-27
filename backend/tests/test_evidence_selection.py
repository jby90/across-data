import hashlib

import pytest
from pydantic import ValidationError

from app.analysis.evidence_selection import AnalysisResponse, select_quote, sentence_catalog
from app.analysis.tools import verify_finding


@pytest.mark.parametrize(
    "text",
    [
        "An organisation must protect personal data.\r\nIt must cease retention when no longer needed.",
        "يجب حماية البيانات الشخصية؛ لا يجوز إفشاؤها دون موافقة.\n\nالنقل خارج البلاد يتطلب ترخيصا.",
        "Les données doivent être exactes. Leur durée de conservation doit être limitée.",
        "应保护个人信息。 不得将业务的180天留存周期当作法定期限。\n\n根据规定落实保护措施。",
    ],
)
def test_offsets_preserve_multilingual_source_verbatim(text):
    catalog = sentence_catalog(text)
    selected = select_quote(text, [s["sentence_id"] for s in catalog])
    assert selected["quote"] == text
    assert selected["source_text_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    for s in catalog:
        assert text[s["start"] : s["end"]] == s["text"]


@pytest.mark.parametrize("ids", [[0], [999], [1, 1], [2, 1], [1, 3], [True], []])
def test_invalid_selections_cannot_invent_or_join_disjoint_quotes(ids):
    with pytest.raises(ValueError):
        select_quote(
            "First statement of protection. Second statement of consent. Third statement.", ids
        )


def test_schema_rejects_model_supplied_quotes_and_vacuous_success():
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate({"findings": []})
    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate({"findings": [{"quote": "fabrication"}]})


def test_numeric_claim_must_be_supported_by_selected_quote_not_other_sentence():
    text = "An organisation must protect personal data. The maximum penalty is 10% of turnover."
    selected = select_quote(text, [1])
    ok, _ = verify_finding({**selected, "obligation": "最高罚款10%"}, text)
    assert not ok
    ok, _ = verify_finding({**select_quote(text, [2]), "obligation": "最高罚款10%"}, text)
    assert ok
