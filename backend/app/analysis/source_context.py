"""Read-only, archive-proven fragment links. Labels alone never prove ancestry."""

import re
from functools import lru_cache

from app.ingestion.clauses import split_clauses
from app.ingestion.parsing import parse_document


def compact(text):
    return re.sub(r"\s+", " ", text).strip()


def prefix_end(prefix, source):
    """Exact ordered lines tolerate inserted running headers, never changed letters.

    This proves navigation membership only. It cannot supply a stitched quotation.
    """
    lines = [compact(line) for line in prefix.splitlines() if line.strip()]
    body = compact(source)
    if not lines or not body.startswith(lines[0]):
        return None
    end = 0
    for line in lines:
        start = body.find(line, end)
        if start < 0:
            return None
        end = start + len(line)
    return end


@lru_cache(maxsize=16)
def source_drafts(content: bytes, mime: str):
    parsed = parse_document(content, mime)
    return split_clauses(parsed.pages)[0]


def fragment_links(candidates, drafts):
    """Require the unchanged root prefix AND continuation in one parsed provision.

    This is a navigation link, not permission to use the root as the continuation's
    evidence. Real decimal provisions and ambiguous source matches stay independent.
    """
    links = {}
    roots = [c for c in candidates if c.heading]
    for child in candidates:
        possible = [
            root
            for root in roots
            if child.label.startswith(root.label + ".")
            and child.heading == root.heading
            and child.section_path == root.section_path
            and child.version_id == root.version_id
        ]
        proven = []
        for root in possible:
            body = compact(child.text)
            matches = [
                d
                for d in drafts
                if d.clause_number == root.label
                and d.section_path == root.section_path
                and (end := prefix_end(root.text, d.text)) is not None
                and compact(d.text).find(body) >= end
            ]
            if len(matches) == 1:
                proven.append(root)
        if len(proven) == 1:
            links[child.clause_id] = proven[0].clause_id
    return links


def include_context_roots(scored, candidates, top_k):
    """Context occupies a real result slot; no hidden expansion of recall@k."""
    by_id = {c.clause_id: c for c in candidates}
    out, seen = [], set()
    for candidate, score in scored:
        for row in (candidate, by_id.get(candidate.context_parent_id)):
            if row and row.clause_id not in seen and len(out) < top_k:
                out.append((row, score))
                seen.add(row.clause_id)
    return out
