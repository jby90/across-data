"""Source-bound comparison slots, not edition merging or a claim of current law."""

import re
from collections import Counter


def norm(value):
    digits = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    return re.sub(r"\s+", " ", value or "").strip().casefold().translate(digits)


def counterpart_key(row):
    if not row.instrument_identity or not row.instrument_role or not row.label:
        return None
    return row.instrument_identity, row.instrument_role, norm(row.label)


def include_source_alternatives(scored, candidates, top_k):
    """Reserve real top-k slots for a corresponding provision of another source.

    Only archive-proven, explicitly registered identities may form a group. An
    ambiguous number repeated in annexes never matches by its number alone.
    Nothing is marked valid, equivalent, translated or legally approved here.
    """
    counts = Counter((counterpart_key(c), c.document_id) for c in candidates)
    out, seen = [], set()
    for row, score in scored:
        if len(out) >= top_k:
            break
        if row.clause_id not in seen:
            out.append((row, score))
            seen.add(row.clause_id)
        key = counterpart_key(row)
        if key is None or counts[key, row.document_id] != 1:
            continue
        alternatives = [
            c
            for c in candidates
            if counterpart_key(c) == key
            and c.document_id != row.document_id
            and counts[key, c.document_id] == 1
            and (
                not c.section_path
                or not row.section_path
                or norm(c.section_path) == norm(row.section_path)
            )
        ]
        # Native text is a useful comparison source, never proof it supersedes PDF.
        alternatives.sort(key=lambda c: (c.text_source == "ocr", c.clause_id))
        for other in alternatives[:1]:
            if other.clause_id not in seen and len(out) < top_k:
                out.append((other, score))
                seen.add(other.clause_id)
    return out
