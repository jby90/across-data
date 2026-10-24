"""通用条款切分。

识别的编号样式:
- 新加坡法条:`26.—(1) ...`、`26A. ...`(条标题在编号行的前一行)、附表段落 `20. ...`
- 指引段落号:`19.4` 单独成行,正文在后面几行
- 英文 Section/Article/Regulation/Rule N、法文 Article N/premier、中文 第N条、阿拉伯文 المادة N

可靠性措施(法条定位准确率的关键):
- 编号必须递增才算新条款,正文里的列表项("1. ...")不会被误当成条款;
- 一份文件内数字编号的"层级模式"锁定(单级如 26 / 两级如 19.4),不混用;
- PART/CHAPTER/SCHEDULE 作为结构标记,写入每条的 section_path,不混进条款正文;
- 目录页(每条只有一行标题)识别后合并为一个"目录"块,不当作条款;
- 原文逐字保留。识别不出编号时退化为按段落切分,并在 strategy 里如实标明。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ingestion.source_layout import atomic_line_blocks

_ARABIC_DIGITS = "٠-٩"
_CN_NUM = "一二三四五六七八九十百千零〇两"

# 关键词式/中文/阿拉伯文编号:无需递增校验
_KEYWORD_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"^(?:Section|SECTION|Article|ARTICLE|Art\.|Regulation|REGULATION|Rule|RULE|Clause|CLAUSE)\s+"
        r"(?P<num>\d+[A-Za-z]?(?:\.\d+)*|[Pp]remier|1er)\b[\s.:—–-]*(?P<rest>.*)$",
    ),
    re.compile(rf"^第(?P<num>[{_CN_NUM}\d]+)条[\s、.:：]*(?P<rest>.*)$"),
    re.compile(rf"^(?:المادة|مادة)\s+(?P<num>[\d{_ARABIC_DIGITS}]+|\S+)[\s:：.\-–—]*(?P<rest>.*)$"),
]
# 新加坡法条:26.—(1) / 26A. In this ... / 15B. In proceedings ...
# 点号后必须跟空格或破折号:避免把日期 31.12.2021 误当成第 31 条
_SG_SECTION = re.compile(r"^(?P<num>\d{1,3}[A-Z]{0,3})\.(?:\s*[—–-]\s*|\s+)(?P<rest>\S.*)$")
# 两级编号:1.1 text / 19.4(正文可在后面行)
_DECIMAL = re.compile(r"^(?P<num>\d{1,3}\.\d{1,3}(?:\.\d{1,3})?)[.)]?(?:\s+(?P<rest>\S.*))?\s*$")
# 通用"1. Text"(要求首字母大写/引号/括号开头,减少误判)
_SIMPLE = re.compile(r"^(?P<num>\d{1,3})[.)]\s+(?P<rest>[A-Z“\"(].*)$")

_PART = re.compile(
    r"^(?P<kind>PART|CHAPTER|DIVISION|Part|Chapter)\s+(?P<id>\d+[A-Z]?|[IVXLC]+[A-Z]?)\b"
    r"[\s:.—–-]*(?P<title>.*)$"
)
_SCHEDULE = re.compile(
    r"^(?:THE\s+)?(?:(?:FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH)\s+)?"
    r"SCHEDULE\b(?P<rest>.*)$"
)
_DIVISION = re.compile(r"^(?P<label>Division\s+\d+[A-Z]?\s*[—–-]\s*\S.*)$")
_CONTINUED = re.compile(r"continued\s*$", re.IGNORECASE)
_TOC_LEADER = re.compile(r"\.{4,}|…{2,}")

_MAX_HEADING_LEN = 120
_HEADING_LINE_MAX = 80
_MAX_JUMP = 30
_SPARSE_AVG_CHARS = 8000  # 平均每条超过这个字数且很稀疏,说明编号不可靠
_CHUNK_TARGET = 2500


@dataclass
class ClauseDraft:
    ordinal: int
    clause_number: str | None
    heading: str | None
    text: str
    page_start: int
    page_end: int
    section_path: str | None = None


def match_header(line: str) -> tuple[str, str | None] | None:
    """无状态地判断一行是不是条款开头。返回 (编号, 小标题) 或 None。

    注意:数字编号样式在真实切分时还要经过递增校验(见 split_clauses),这里只做样式匹配。
    """
    stripped = line.strip()
    for pattern in _KEYWORD_PATTERNS:
        m = pattern.match(stripped)
        if m:
            rest = m.group("rest").strip()
            heading = rest if rest and len(rest) <= _MAX_HEADING_LEN else None
            return m.group("num"), heading
    for pattern in (_DECIMAL, _SG_SECTION, _SIMPLE):
        m = pattern.match(stripped)
        if m:
            return m.group("num"), None
    return None


_AR_TO_ASCII = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _key(num: str) -> tuple[int, int, str]:
    """编号的排序键:26A -> (26,0,'A');19.4 -> (19,4,'');15.12.3 取前两级;premier -> (1,0,'')。"""
    num = num.translate(_AR_TO_ASCII)
    if num.lower() in ("premier", "1er"):
        return 1, 0, ""
    if "." in num:
        parts = num.split(".")
        return int(parts[0]), int(parts[1]), ""
    m = re.match(r"^(\d+)([A-Z]*)$", num)
    if not m:
        raise ValueError(num)
    return int(m.group(1)), 0, m.group(2)


def _numeric_header(line: str) -> tuple[str, str, str] | None:
    """数字式编号头。返回 (编号, 模式 single/decimal, 行内剩余文字)。"""
    s = line.strip()
    m = _DECIMAL.match(s)
    if m:
        return m.group("num"), "decimal", (m.group("rest") or "")
    m = _SG_SECTION.match(s)
    if m:
        return m.group("num"), "single", m.group("rest")
    m = _SIMPLE.match(s)
    if m:
        return m.group("num"), "single", m.group("rest")
    return None


def _looks_like_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > _HEADING_LINE_MAX:
        return False
    if s[0].islower() or s[0] in "(“\"'—–-0123456789[":
        return False
    if s[-1] in ".;:,—–-":
        return False
    if _PART.match(s) or _SCHEDULE.match(s) or _TOC_LEADER.search(s):
        return False
    return True


def _is_toc_entry(line: str) -> bool:
    """像目录条目的行:PART/Division 标记,或"编号 + 不带句末标点的短标题"。"""
    if _PART.match(line) or _DIVISION.match(line):
        return True
    nh = _numeric_header(line)
    if nh is None:
        return False
    rest = nh[2].strip()
    if not rest:  # 只有编号、没有标题(如指引里单独成行的 19.4):是正文段落号,不是目录条目
        return False
    return len(rest) <= 70 and not rest.endswith((".", ";", ":", ",", "—", "–"))


def _toc_pages(pages: list[str]) -> set[int]:
    """识别目录页(页码从 1 起):多数行是"编号 + 短标题",或带点线页码的短行。

    目录页的文字并入一个"目录"块,不参与条款识别。
    """
    toc: set[int] = set()
    prev_is_toc = False
    for n, page in enumerate(pages, start=1):
        lines = [ln.strip() for ln in page.splitlines() if ln.strip()]
        is_toc = False
        if len(lines) >= 4:
            starts = sum(1 for ln in lines if _is_toc_entry(ln))
            dotted = sum(1 for ln in lines if _TOC_LEADER.search(ln))
            long_lines = sum(1 for ln in lines if len(ln) > 100)
            if long_lines == 0:
                threshold = 0.3 if prev_is_toc else 0.4
                is_toc = starts / len(lines) >= threshold or dotted / len(lines) >= 0.4
            if len(lines) < 8 and not prev_is_toc:
                is_toc = False
        if is_toc:
            toc.add(n)
        prev_is_toc = is_toc
    return toc


def _longest_chain(cands: list[tuple[int, str, str, tuple[int, int, str], int]]) -> list[int]:
    """在候选(行号, 编号, 模式, 排序键, 段)中,选出最长的"严格递增且每步跳跃不大"的链,返回候选下标。

    比逐个判断更稳:修订法案里引用的"新增条文"(如 29A.—(1))编号会和正文编号交错,
    逐个判断会被它带偏,取最长链则能保住真正的正文序列。
    """
    n = len(cands)
    if n == 0:
        return []
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        ki = cands[i][3]
        for j in range(i):
            kj = cands[j][3]
            if ki > kj and ki[0] - kj[0] <= _MAX_JUMP and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    end = max(range(n), key=lambda i: (best[i], -i))
    chain = []
    while end != -1:
        chain.append(end)
        end = prev[end]
    return chain[::-1]


def _choose_numeric(flat: list[tuple[int, str]], toc_pages: set[int]) -> tuple[set[int], int]:
    """决定哪些数字编号行是真正的条款开头。返回 (被接受的行下标集合, 接受的数量)。"""
    cands: list[tuple[int, str, str, tuple[int, int, str], int]] = []
    seg = 0
    for idx, (page_no, line) in enumerate(flat):
        if page_no in toc_pages:
            continue
        s = line.strip()
        sm = _SCHEDULE.match(s)
        if sm and not _CONTINUED.search(s) and s.upper() == s:
            seg += 1  # 附表是新的编号段
            continue
        if _PART.match(s) or _DIVISION.match(s):
            continue
        nh = _numeric_header(s)
        if nh is None:
            continue
        try:
            key = _key(nh[0])
        except ValueError:
            continue
        cands.append((idx, nh[0], nh[1], key, seg))

    accepted: set[int] = set()
    for seg_id in sorted({c[4] for c in cands}):
        in_seg = [c for c in cands if c[4] == seg_id]
        best_pick: list[int] = []
        for mode in ("single", "decimal"):
            pool = [c for c in in_seg if c[2] == mode]
            picked: list[int] = []
            remaining = pool
            for round_no in range(4):  # 第一条链是主序列;之后只接受从 1 重新开始、且足够长的序列
                chain = _longest_chain(remaining)
                if not chain:
                    break
                members = [remaining[k] for k in chain]
                if round_no > 0 and not (len(members) >= 5 and members[0][3][0] == 1):
                    break
                picked.extend(m[0] for m in members)
                taken = {m[0] for m in members}
                remaining = [c for c in remaining if c[0] not in taken]
            if len(picked) > len(best_pick):
                best_pick = picked
        accepted.update(best_pick)
    return accepted, len(accepted)


@dataclass
class _Raw:
    number: str | None
    heading: str | None
    lines: list[str]
    page_start: int
    page_end: int
    path: str | None
    seq: int  # 所属编号序列(目录/正文/附表各是一个序列)


def _split_numbered(pages: list[str]) -> list[_Raw]:
    raws: list[_Raw] = []
    current: _Raw | None = None
    path: str | None = None
    part_label: str | None = None
    division_label: str | None = None
    schedule_label: str | None = None
    toc_pages = _toc_pages(pages)
    toc_raw: _Raw | None = None
    last_key: tuple[int, int, str] | None = None
    kw_last: tuple[int, int, str] | None = None
    seq = 0

    def compute_path() -> str | None:
        return " / ".join(x for x in (schedule_label, part_label, division_label) if x) or None

    def new_sequence() -> None:
        nonlocal last_key, kw_last, seq
        last_key, kw_last = None, None
        seq += 1

    flat: list[tuple[int, str]] = []
    atomic = {}
    for page_no, page in enumerate(pages, start=1):
        lines = [line.rstrip() for line in page.splitlines() if line.strip()]
        blocks = atomic_line_blocks("\n".join(lines))
        base = len(flat)
        for start, (end, label) in blocks.items():
            atomic[base + start] = (base + end, label)
        if blocks:
            toc_pages.discard(page_no)
        flat.extend((page_no, line) for line in lines)

    accepted_numeric, numeric_count = _choose_numeric(flat, toc_pages)
    keyword_count = sum(
        1
        for pg, ln in flat
        if pg not in toc_pages and any(pat.match(ln.strip()) for pat in _KEYWORD_PATTERNS)
    )
    numeric_style = numeric_count >= 2 and numeric_count >= keyword_count

    i = 0
    while i < len(flat):
        page_no, line = flat[i]
        s = line.strip()

        if i in atomic:
            end, label = atomic[i]
            new_sequence()
            raws.append(
                _Raw(
                    None,
                    label,
                    [ln for _, ln in flat[i:end]],
                    page_no,
                    flat[end - 1][0],
                    label,
                    seq,
                )
            )
            current = None
            i = end
            continue

        # ---- 目录页:整页并入"目录"块,不参与条款识别 ----
        if page_no in toc_pages:
            if toc_raw is None:
                toc_raw = _Raw(None, "目录(自动识别)", [], page_no, page_no, None, seq)
                raws.append(toc_raw)
            toc_raw.lines.append(line)
            toc_raw.page_end = page_no
            current = None
            i += 1
            continue

        # ---- 结构标记:附表 ----
        sm = _SCHEDULE.match(s)
        if sm and _CONTINUED.search(s):  # "THE SCHEDULE — continued" 是续页标记,丢弃
            i += 1
            continue
        if sm and s.upper() == s:
            schedule_label = s
            part_label = None
            division_label = None
            path = compute_path()
            new_sequence()
            current = None
            i += 1
            continue

        # ---- 结构标记:PART/CHAPTER ----
        pm = _PART.match(s)
        if (
            pm
            and not _TOC_LEADER.search(s)
            and (pm.group("kind").isupper() or not pm.group("title"))
        ):
            title_parts = [pm.group("title").strip()] if pm.group("title").strip() else []
            consumed = 1
            j = i + 1
            while j < len(flat) and len(title_parts) < 3 and flat[j][0] not in toc_pages:
                nxt = flat[j][1].strip()
                if (
                    nxt
                    and nxt.upper() == nxt
                    and any(ch.isalpha() for ch in nxt)
                    and len(nxt) <= 80
                    and not _numeric_header(nxt)
                    and not _PART.match(nxt)
                    and not _SCHEDULE.match(nxt)
                ):
                    title_parts.append(nxt)
                    consumed += 1
                    j += 1
                else:
                    break
            title = " ".join(title_parts)
            part_label = f"{pm.group('kind').upper()} {pm.group('id')}" + (
                f" {title}" if title else ""
            )
            division_label = None
            path = compute_path()
            current = None
            i += consumed
            continue

        # ---- 结构标记:Division ----
        dm = _DIVISION.match(s)
        if dm and not _TOC_LEADER.search(s):
            division_label = dm.group("label")
            path = compute_path()
            current = None
            i += 1
            continue

        # ---- 关键词/中文/阿拉伯文编号 ----
        header: tuple[str, str | None] | None = None
        for pattern in _KEYWORD_PATTERNS:
            m = pattern.match(s)
            if m:
                rest = m.group("rest").strip()
                header = (m.group("num"), rest if rest and len(rest) <= _MAX_HEADING_LEN else None)
                break
        if header is not None:
            try:
                hkey: tuple[int, int, str] | None = _key(header[0])
            except ValueError:
                hkey = None  # 中文数字等无法比较:不做递增校验
            ok = True
            if numeric_style:
                ok = False  # 本文件用数字编号,行首的 "Article 29 of ..." 是交叉引用
            elif hkey is not None and kw_last is not None:
                if hkey > kw_last and hkey[0] - kw_last[0] <= _MAX_JUMP:
                    ok = True
                elif hkey[0] == 1 and hkey[1] <= 1 and kw_last[0] > 3 and not hkey[2]:
                    new_sequence()  # 回到 1:新的序列(如附件)
                else:
                    ok = False  # 行首的交叉引用(如 "Article 29 of the GDPR ..."),不是新条款
            if ok:
                if hkey is not None:
                    kw_last = hkey
                current = _Raw(header[0], header[1], [line], page_no, page_no, path, seq)
                raws.append(current)
                i += 1
                continue

        # ---- 数字式编号(需递增校验)----
        nh = _numeric_header(s)
        if nh is not None and numeric_style and i in accepted_numeric:
            num, nmode, _rest = nh
            key = _key(num)
            accept = True
            if accept:
                if last_key is not None and key < last_key:
                    new_sequence()  # 编号回落:新的序列(如附件、正文跟在目录之后)
                last_key = key
                heading_lines: list[str] = []
                if nmode == "single" and current is not None:
                    # 条标题是编号行的前一行(最多两行,以大写开头、不以句号等结尾)
                    floor = 1 if current.number is not None else 0
                    while (
                        len(heading_lines) < 2
                        and len(current.lines) > floor
                        and _looks_like_heading(current.lines[-1])
                    ):
                        heading_lines.insert(0, current.lines.pop().strip())
                head_text = " ".join(heading_lines) if heading_lines else None
                current = _Raw(num, head_text, [*heading_lines, line], page_no, page_no, path, seq)
                current.page_start = page_no
                raws.append(current)
                i += 1
                continue

        # ---- 普通行 ----
        if current is None:
            current = _Raw(None, None, [], page_no, page_no, path, seq)
            raws.append(current)
        current.lines.append(line)
        current.page_end = page_no
        i += 1
    return raws


def _chunk_by_size(pages: list[str]) -> list[ClauseDraft]:
    """没有可靠条款编号时,按约 2500 字分块(尽量在句末断开),记录页码范围。条款编号为空。"""
    drafts: list[ClauseDraft] = []
    block: list[str] = []
    size = 0
    start = end = 1
    for page_no, page in enumerate(pages, start=1):
        for line in page.splitlines():
            if not line.strip():
                continue
            if not block:
                start = page_no
            block.append(line.rstrip())
            end = page_no
            size += len(line)
            if size >= _CHUNK_TARGET and line.rstrip().endswith((".", ":", ";", "?", "”")):
                drafts.append(ClauseDraft(len(drafts), None, None, "\n".join(block), start, end))
                block, size = [], 0
    if block:
        drafts.append(ClauseDraft(len(drafts), None, None, "\n".join(block), start, end))
    return drafts


def _build(raws: list[_Raw]) -> list[ClauseDraft]:
    drafts: list[ClauseDraft] = []
    for r in raws:
        text = "\n".join(r.lines).strip()
        if not text:
            continue
        drafts.append(
            ClauseDraft(
                ordinal=len(drafts),
                clause_number=r.number,
                heading=r.heading,
                text=text,
                page_start=r.page_start,
                page_end=r.page_end,
                section_path=r.path,
            )
        )
    return drafts


def split_clauses(
    pages: list[str], *, paragraph_mode: str = "blank"
) -> tuple[list[ClauseDraft], str]:
    """返回 (条款列表, 策略名)。strategy: numbered / paragraph / line / empty。

    paragraph_mode: 退化为段落切分时的依据,blank=空行(文本),line=每行(HTML)。
    """
    raws = _split_numbered(pages)
    numbered = [r for r in raws if r.number is not None]
    if any(r.path and r.path.startswith(("TABLE /", "REFERENCE /")) for r in raws):
        return _build(raws), "layout"
    if len(numbered) >= 2:
        total_chars = sum(len(pg) for pg in pages)
        sparse = (
            len(pages) >= 20
            and len(numbered) / len(pages) < 0.15
            and total_chars / len(numbered) > _SPARSE_AVG_CHARS
        )
        if not sparse:
            return _build(raws), "numbered"
        if paragraph_mode == "blank":
            return _chunk_by_size(pages), "chunk"

    paras: list[ClauseDraft] = []
    block: list[str] = []
    start = end = 1

    def flush() -> None:
        nonlocal block
        text = "\n".join(block).strip()
        if text:
            paras.append(ClauseDraft(len(paras), None, None, text, start, end))
        block = []

    for page_no, page in enumerate(pages, start=1):
        for line in page.splitlines():
            if not line.strip():
                if paragraph_mode == "blank":
                    flush()
                continue
            if paragraph_mode == "line":
                flush()
            if not block:
                start = page_no
            block.append(line.rstrip())
            end = page_no
    flush()
    if not paras:
        return [], "empty"
    if paragraph_mode == "blank" and len(paras) == 1 and len(pages) > 1:
        # PDF 文字里没有可靠的空行:按页切分,并如实标明
        per_page = [
            ClauseDraft(n, None, None, p.strip(), n + 1, n + 1)
            for n, p in enumerate(pages)
            if p.strip()
        ]
        for i, d in enumerate(per_page):
            d.ordinal = i
        return per_page, "page"
    return paras, "paragraph" if paragraph_mode == "blank" else "line"
