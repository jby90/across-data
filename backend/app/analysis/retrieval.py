"""Query-aware multilingual sparse retrieval. Normalization is never used as legal evidence."""

import math
import re
import unicodedata
from collections import Counter

RETRIEVAL_VERSION = "query-bm25-rrf-v1"
_CJK = re.compile(r"[\u3400-\u9fff]+")
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_index(text: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text or "").casefold()
        if unicodedata.category(c) not in ("Mn", "Me", "Cf") and c != "\u0640"
    )


def terms(text: str) -> Counter:
    normalized = normalize_index(text)
    out = []
    for match in _CJK.finditer(normalized):
        s = match.group()
        out.extend(s[i : i + 2] for i in range(len(s) - 1))
        if len(s) == 1:
            out.append(s)
    for word in _WORDS.findall(_CJK.sub(" ", normalized)):
        if word.isdecimal():  # user-supplied dates/amounts are not legal query topics
            continue
        out.append(word)
        if word.isascii() and len(word) > 5:
            out.append("prefix:" + word[:5])
    return Counter(out)


class SparseIndex:
    def __init__(self, texts):
        self.rows = [terms(t) for t in texts]
        self.lengths = [sum(r.values()) for r in self.rows]
        self.average = sum(self.lengths) / max(1, len(self.rows)) or 1
        self.df = Counter(t for row in self.rows for t in row)

    def scores(self, query: str):
        wanted = terms(query)
        scores = []
        n = len(self.rows)
        for row, length in zip(self.rows, self.lengths, strict=True):
            score = 0.0
            for term in wanted:
                tf = row.get(term, 0)
                if tf:
                    idf = math.log(1 + (n - self.df[term] + 0.5) / (self.df[term] + 0.5))
                    score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / self.average))
            scores.append(score)
        return scores


class ClauseIndex:
    def __init__(self, candidates):
        self.candidates = candidates
        # Pending machine translations only find candidates; source and review status stay separate.
        self.body = SparseIndex([c.text + "\n" + c.translated_text for c in candidates])
        self.heading = SparseIndex(
            [
                c.heading + "\n" + c.text.split("\n", 1)[0] + "\n" + c.translated_text[:180]
                for c in candidates
            ]
        )

    def rank(self, dimension_terms: str, query: str | None):
        channels = [
            (self.body.scores(dimension_terms), 1.0),
            (self.heading.scores(dimension_terms), 0.5),
        ]
        if query and terms(query):
            channels += [(self.body.scores(query), 3.0), (self.heading.scores(query), 1.0)]
        fused = [0.0] * len(self.candidates)
        for scores, weight in channels:
            order = sorted(
                (i for i, s in enumerate(scores) if s > 0),
                key=lambda i: (
                    -scores[i],
                    self.candidates[i].clause_id,
                ),
            )
            for rank, index in enumerate(order, 1):
                fused[index] += weight / (60 + rank)
        order = sorted(
            (i for i, s in enumerate(fused) if s > 0),
            key=lambda i: (
                -fused[i],
                self.candidates[i].clause_id,
            ),
        )
        return [(self.candidates[i], fused[i]) for i in order]


def task_query(facts: dict, dimension: str) -> str:
    from app.analysis.workflow_engine import REQUIRED_FACTS_SPEC

    keys = {f["fact_key"] for f in REQUIRED_FACTS_SPEC if f["dimension_code"] == dimension}
    selected = [v for k, v in facts.items() if k in keys and isinstance(v, str)]
    # Free-form questions, new business schemas and dimensions without a dedicated slot stay usable.
    known = {f["fact_key"] for f in REQUIRED_FACTS_SPEC}
    free_form = [v for k, v in facts.items() if k not in known and isinstance(v, str)]
    # A filled retention slot must not turn a sanctions query into a retention search.
    return "\n".join(selected or free_form)[:6000]
