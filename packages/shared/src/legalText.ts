export interface LegalTerm {
  rendering_label?: string
  id: string
  source_term: string
  target_zh: string
  aliases?: string[]
  concept_code?: string
  definition_original?: string | null
  definition_zh?: string | null
  source_label: string
  source_reference: string
  source_url?: string | null
  document_id?: string | null
  version_id?: string | null
  source_kind?: string
  alternatives?: LegalTerm[]
  clause_id?: string | null
  source_scope?: string
  scope_path?: string
}
export function splitLegalText(
  text: string,
  terms: LegalTerm[],
): Array<{ text: string; term?: LegalTerm }> {
  const priority = (term: LegalTerm) =>
    ({
      law_definition: 1,
      regulator_guidance: 2,
      official_parallel: 3,
      international_reference: 4,
    })[term.source_kind || ''] || 5
  const privacyContext =
    /personal data|personal information|données personnelles|données à caractère personnel|البيانات الشخصية|المعطيات الشخصية|个人数据|个人信息/.test(
      text.toLowerCase(),
    )
  const candidates = terms
    .flatMap((term) =>
      [term.target_zh, term.source_term, ...(term.aliases || [])]
        .filter((t) => t && t.length > 1 && !t.includes(' / '))
        .filter(
          (t) =>
            privacyContext ||
            term.source_kind === 'law_definition' ||
            !['data_controller', 'data_processor', 'processing'].includes(
              term.concept_code || '',
            ) ||
            !/^[\p{Script=Latin}\p{Script=Arabic}]/u.test(t),
        )
        .map((alias) => ({ alias, term })),
    )
    .sort((a, b) => priority(a.term) - priority(b.term) || b.alias.length - a.alias.length)
  const out: Array<{ text: string; term?: LegalTerm }> = []
  let start = 0,
    cursor = 0
  const lower = text.toLocaleLowerCase()
  while (cursor < text.length) {
    const hit = candidates.find(({ alias }) => {
      if (!lower.startsWith(alias.toLocaleLowerCase(), cursor)) return false
      if (/^[\p{Script=Latin}\p{Script=Arabic}]/u.test(alias))
        return (
          !/[\p{L}\p{N}]/u.test(text[cursor - 1] || '') &&
          !/[\p{L}\p{N}]/u.test(text[cursor + alias.length] || '')
        )
      return true
    })
    if (!hit) {
      cursor++
      continue
    }
    if (cursor > start) out.push({ text: text.slice(start, cursor) })
    const alternatives = [
      ...new Map(
        candidates
          .filter(
            (c) =>
              c.alias.toLowerCase() === hit.alias.toLowerCase() &&
              priority(c.term) === priority(hit.term),
          )
          .map((c) => [`${c.term.definition_original}|${c.term.source_reference}`, c.term]),
      ).values(),
    ]
    out.push({
      text: text.slice(cursor, cursor + hit.alias.length),
      term: alternatives.length > 1 ? { ...hit.term, alternatives } : hit.term,
    })
    cursor += hit.alias.length
    start = cursor
  }
  if (start < text.length) out.push({ text: text.slice(start) })
  return out
}
