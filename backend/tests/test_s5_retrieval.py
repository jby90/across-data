from dataclasses import replace

import pytest

from app.analysis.tools import ClauseCandidate, clause_relevance


def candidate(text, title="Unrelated Gazette", heading=""):
    return ClauseCandidate(
        "c",
        "d",
        title,
        "v",
        1,
        "7",
        text,
        "https://official.example/7",
        "pending_review",
        "unknown",
        heading=heading,
    )


@pytest.mark.parametrize(
    "term,heading,body",
    [
        (
            "retention",
            "Retention of personal data",
            "Retention must end when purpose is exhausted.",
        ),
        ("conservation", "Durée de conservation", "La durée est limitée aux finalités déclarées."),
        ("نقل", "نقل البيانات", "يجب ضمان حماية البيانات عند نقلها إلى الخارج."),
    ],
)
def test_headings_rank_rules_above_title_only_and_verbose_mentions(term, heading, body):
    relevant = candidate(heading + "\n" + body, heading=heading)
    irrelevant = candidate("Procedural provisions without the relevant obligation.", title=term)
    noisy = candidate(("A reference to " + term + " in procedural material. ") * 20)
    assert clause_relevance(irrelevant, [term])[0] == 0
    assert clause_relevance(relevant, [term])[0] > clause_relevance(noisy, [term])[0]
    # Country, clause ID, number and document name are not successful ranking special cases.
    changed = replace(relevant, clause_id="different", label="999", document_title="Other law")
    assert clause_relevance(changed, [term]) == clause_relevance(relevant, [term])
