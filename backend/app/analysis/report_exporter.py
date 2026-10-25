from __future__ import annotations

import copy
import io
import re
import textwrap
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont

from app.analysis.citation_policy import policy_label, review_detail, review_label, review_overview
from app.analysis.models import AnalysisTask, ReportVersion
from app.analysis.schemas import format_conversation_code

DIMENSIONS = tuple(f"D{i}" for i in range(1, 9))
EXPORT_NOTICE = "机器生成草稿；机器翻译及人工复核状态见引用标记，程序核验不等于法律审批。"
DISCLAIMER = "本报告供业务研究参考，不构成法律意见；业务事实及适用法律须由负责人和专业人员核对。"


def export_filename(task, rv, ext, dimension=None):
    country = re.sub(r"[^A-Za-z0-9_-]", "_", task.country_code)
    return f"cyshuhe_{country}_{dimension or '8dim'}_report_v{rv.version_no}.{ext}"


def _view(task, rv, dimension):
    """Never borrow the current task's findings when exporting a historical version."""
    if dimension is not None and dimension not in DIMENSIONS:
        raise ValueError("未知报告维度")
    dims = copy.deepcopy(rv.dimensions_json or [])
    if dimension:
        dims = [d for d in dims if d.get("dimension_code") == dimension]
        if not dims:
            raise ValueError("该报告版本没有此维度")
    findings = [f for d in dims for f in d.get("findings", [])]
    task_view = SimpleNamespace(
        id=task.id,
        created_at=task.created_at,
        title=task.title,
        country_code=task.country_code,
        vehicle_model=task.vehicle_model,
        findings_json=findings,
    )
    rv_view = SimpleNamespace(
        citation_policy=getattr(rv, "citation_policy", None),
        version_no=rv.version_no,
        is_partial_sample=rv.is_partial_sample,
        dimensions_json=dims,
        executive_summary=(
            f"本文件仅导出 {dimension} {dims[0]['dimension_title']}。"
            if dimension
            else rv.executive_summary
        ),
    )
    return task_view, rv_view


def _context(task, rv, dimension):
    code = format_conversation_code(task.id, task.created_at)
    return f"{code} | 报告 v{rv.version_no} | {dimension or '完整报告'}"


def build_report_zip(task, rv):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(export_filename(task, rv, "docx"), build_report_docx(task, rv))
        for code in DIMENSIONS:
            zf.writestr(export_filename(task, rv, "docx", code), build_report_docx(task, rv, code))
    return buf.getvalue()


COUNTRY_LABEL = {
    "SG": "新加坡 (Singapore)",
    "EG": "埃及 (Egypt)",
    "TN": "突尼斯 (Tunisia)",
    "MA": "摩洛哥 (Morocco)",
    "LB": "黎巴嫩 (Lebanon)",
}

# 随包携带的开源字体(Source Han Sans,Apache-2.0),不依赖系统字体
BUNDLED_FONT = Path(__file__).parent / "fonts" / "SourceHanSansCN-Medium.ttf"

PARTIAL_NOTICE = (
    "【部分样稿】部分维度缺少可通过原文核验的依据,已逐维标注缺口;"
    "引用的中文为机器翻译,尚未经律所人工复核。"
)


def _w_p(text: str, bold: bool = False, size: int = 22, color: str = "1E293B") -> str:
    b_tag = "<w:b/>" if bold else ""
    keep = "<w:keepNext/>" if (bold and size >= 24) or text.startswith("依据与合规义务") else ""
    return (
        f"<w:p><w:pPr>{keep}<w:spacing w:after='120' w:line='300' w:lineRule='auto'/></w:pPr>"
        f"<w:r><w:rPr>{b_tag}<w:color w:val='{color}'/>"
        f"<w:sz w:val='{size}'/><w:szCs w:val='{size}'/></w:rPr>"
        f"<w:t xml:space='preserve'>{escape(text or '')}</w:t></w:r></w:p>"
    )


def _w_tc(text: str, bold: bool = False, shade: str | None = None) -> str:
    shd = f"<w:shd w:val='clear' w:color='auto' w:fill='{shade}'/>" if shade else ""
    return f"<w:tc><w:tcPr>{shd}</w:tcPr>{_w_p(text, bold=bold, size=19)}</w:tc>"


def _clause_ref(f: dict) -> str:
    notice = f"；{f['source_notice']}" if f.get("source_notice") else ""
    return (
        f"{f.get('document_title', '')} {f.get('clause_number', '')} "
        f"(v{f.get('version_no', 1)}){notice}"
    )


def build_report_docx(task: AnalysisTask, rv: ReportVersion, dimension: str | None = None) -> bytes:
    """生成八维度国别安全评估报告 Word (.docx)。"""
    task, rv = _view(task, rv, dimension)
    country = COUNTRY_LABEL.get(task.country_code, task.country_code)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    parts: list[str] = [
        _w_p(
            f"{country} 汽车数据跨境合规评估报告 {dimension or '八维度'}",
            bold=True,
            size=32,
            color="000000",
        ),
        _w_p(
            f"任务:{task.title} | 版本:v{rv.version_no} | 车型:{task.vehicle_model} | {now}",
            size=19,
            color="475569",
        ),
    ]
    parts.extend([_w_p(EXPORT_NOTICE, size=19), _w_p(DISCLAIMER, size=19)])
    parts.append(_w_p(review_overview(rv.dimensions_json), size=19))
    parts.append(_w_p(f"生成时引用策略：{policy_label(rv.citation_policy)}", size=18))
    if rv.is_partial_sample:
        parts.append(_w_p(PARTIAL_NOTICE, bold=True, size=20, color="B45309"))
    parts.append(_w_p("一、执行摘要", bold=True, size=26, color="1D4ED8"))
    parts.extend(_w_p(ln.strip()) for ln in (rv.executive_summary or "").splitlines() if ln.strip())

    parts.append(_w_p("二、合规分析判定矩阵", bold=True, size=26, color="1D4ED8"))
    heads = ["业务事实", "适用条款", "合规义务", "风险等级", "判断依据(原文子串)"]
    rows = ["<w:tr>" + "".join(_w_tc(h, True, "EFF6FF") for h in heads) + "</w:tr>"]
    if not task.findings_json:
        parts.append(
            _w_p("该版本未保存判定矩阵快照；请查阅下列章节及其引用，未使用任务最新结果替代。")
        )
    for f in task.findings_json or []:
        rows.append(
            "<w:tr>"
            + _w_tc(str(f.get("business_fact", "")))
            + _w_tc(_clause_ref(f))
            + _w_tc(str(f.get("compliance_obligation", "")))
            + _w_tc(str(f.get("risk_level", "")), True)
            + _w_tc(f"{f.get('basis_quote', '')}\n【中译(机器翻译)】{f.get('basis_zh', '')}")
            + "</w:tr>"
        )
    border = "<w:{0} w:val='single' w:sz='4' w:space='0' w:color='CBD5E1'/>"
    borders = "".join(
        border.format(n) for n in ("top", "bottom", "left", "right", "insideH", "insideV")
    )
    parts.append(
        "<w:tbl><w:tblPr><w:tblW w:w='5000' w:type='pct'/>"
        f"<w:tblBorders>{borders}</w:tblBorders></w:tblPr>" + "".join(rows) + "</w:tbl>"
    )

    parts.append(_w_p("三、评估章节", bold=True, size=26, color="000000"))
    for dim in rv.dimensions_json or []:
        title = (
            f"【{dim.get('dimension_code', '')}】{dim.get('dimension_title', '')} "
            f"(风险:{dim.get('risk_level', '')} | 状态:{dim.get('status', '')})"
        )
        parts.append(_w_p(title, bold=True, size=24, color="0F172A"))
        if dim.get("missing_gap_note"):
            parts.append(_w_p(f"核查缺口:{dim['missing_gap_note']}", True, 20, "B45309"))
        for line in str(dim.get("content_markdown", "")).splitlines():
            if line.strip():
                parts.append(_w_p(line.strip().lstrip("#").strip()))
        for cite in dim.get("citations", []) or []:
            parts.append(
                _w_p(
                    f"  引用 {cite.get('citation_tag', '')}:{_clause_ref(cite)} — "
                    f'原文:"{cite.get("quote_original", "")}" | 译文:{cite.get("quote_zh", "")}',
                    size=18,
                    color="334155",
                )
            )
            parts.append(
                _w_p(
                    f"来源：{cite.get('source_url') or '未提供'}；"
                    f"人工复核：{review_label(cite)}；"
                    f"{review_detail(cite)}",
                    size=18,
                )
            )

    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document_xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        f"<w:document xmlns:w='{ns}' "
        "xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'><w:body>"
        + "".join(parts)
        + "<w:sectPr><w:headerReference w:type='default' r:id='rIdHeader'/>"
        "<w:pgSz w:w='11906' w:h='16838'/>"
        "<w:pgMar w:top='1800' w:right='1440' w:bottom='1440' w:left='1440' "
        "w:header='360'/></w:sectPr>"
        "</w:body></w:document>"
    )
    rels_ct = "application/vnd.openxmlformats-package.relationships+xml"
    main_ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    content_types = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
        f"<Default Extension='rels' ContentType='{rels_ct}'/>"
        "<Default Extension='xml' ContentType='application/xml'/>"
        f"<Override PartName='/word/document.xml' ContentType='{main_ct}'/></Types>"
    )
    content_types = content_types.replace(
        "</Types>",
        "<Override PartName='/word/header1.xml' "
        "ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml'/></Types>",
    )
    rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    rels = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
        f"<Relationship Id='rId1' Type='{rel_type}' Target='word/document.xml'/></Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr(
            "word/_rels/document.xml.rels",
            "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
            "<Relationship Id='rIdHeader' "
            "Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/header' "
            "Target='header1.xml'/>"
            "</Relationships>",
        )
        zf.writestr(
            "word/header1.xml",
            f"<w:hdr xmlns:w='{ns}'>"
            + _w_p(_context(task, rv, dimension), size=16)
            + _w_p(EXPORT_NOTICE + DISCLAIMER, size=16)
            + "</w:hdr>",
        )
    return buf.getvalue()


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if BUNDLED_FONT.exists():
        return ImageFont.truetype(str(BUNDLED_FONT), size=size)
    return ImageFont.load_default()


def _wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width=width) or [""]


def _pixel_lines(text, font, width):
    """Wrap by rendered width, including long identifiers and mixed-language text."""
    for raw in text.splitlines() or [""]:
        while raw:
            lo, hi = 1, len(raw)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.getlength(raw[:mid]) <= width:
                    lo = mid
                else:
                    hi = mid - 1
            yield raw[:lo]
            raw = raw[lo:]


def build_report_pdf(task: AnalysisTask, rv: ReportVersion, dimension: str | None = None) -> bytes:
    """生成多页 PDF(页面图片式,使用随包开源字体,不依赖系统字体)。"""
    task, rv = _view(task, rv, dimension)
    page_w, page_h = 1240, 1754
    margin_x, margin_top, margin_bottom = 80, 115, 90
    fonts = {
        "TITLE": _load_font(28),
        "H2": _load_font(22),
        "BODY": _load_font(17),
        "SMALL": _load_font(15),
        "WARN": _load_font(15),
    }
    country = COUNTRY_LABEL.get(task.country_code, task.country_code)
    queue: list[tuple[str, str]] = [
        ("TITLE", f"{country} 汽车数据跨境合规评估报告 {dimension or '八维度'}"),
        ("SMALL", f"任务:{task.title} | v{rv.version_no} | 车型:{task.vehicle_model}"),
    ]
    queue.extend([("WARN", EXPORT_NOTICE), ("SMALL", DISCLAIMER)])
    queue.append(("SMALL", review_overview(rv.dimensions_json)))
    queue.append(("SMALL", f"生成时引用策略：{policy_label(rv.citation_policy)}"))
    if rv.is_partial_sample:
        queue += [("WARN", ln) for ln in _wrap(PARTIAL_NOTICE, 50)]
    queue.append(("H2", "一、执行摘要"))
    for raw in (rv.executive_summary or "").splitlines():
        queue += [("BODY", ln) for ln in _wrap(raw.strip(), 50) if raw.strip()]
    queue.append(("H2", "二、合规分析判定矩阵"))
    for idx, f in enumerate(task.findings_json or [], start=1):
        head = (
            f"[{idx}] {f.get('business_fact', '')} → {_clause_ref(f)} "
            f"风险:{f.get('risk_level', '')}"
        )
        queue += [("BODY", ln) for ln in _wrap(head, 50)]
        detail = f"义务:{f.get('compliance_obligation', '')} | 依据:{f.get('basis_quote', '')}"
        queue += [("SMALL", ln) for ln in _wrap(detail, 56)]
    if not task.findings_json:
        queue.append(("SMALL", "该版本未保存判定矩阵快照，请查看本章引用；未借用当前任务数据。"))
    queue.append(("H2", "三、评估章节"))
    for dim in rv.dimensions_json or []:
        title = f"【{dim.get('dimension_code', '')}】{dim.get('dimension_title', '')}"
        queue.append(("H2", f"{title} [{dim.get('risk_level', '')}]"))
        if dim.get("missing_gap_note"):
            queue += [("WARN", ln) for ln in _wrap(f"核查缺口:{dim['missing_gap_note']}", 50)]
        for raw in str(dim.get("content_markdown", "")).splitlines():
            if raw.strip():
                queue += [("BODY", ln) for ln in _wrap(raw.strip().lstrip("#").strip(), 50)]
        for cite in dim.get("citations", []) or []:
            ref = f"引用 {cite.get('citation_tag', '')}: {_clause_ref(cite)}"
            queue += [("SMALL", ln) for ln in _wrap(ref, 56)]
            queue.extend(
                ("SMALL", ln)
                for ln in _wrap(
                    f"原文：{cite.get('quote_original', '')}；"
                    f"来源：{cite.get('source_url') or '未提供'}；"
                    f"人工复核：{review_label(cite)}；"
                    f"{review_detail(cite)}",
                    56,
                )
            )

    steps = {"TITLE": 48, "H2": 42, "WARN": 34, "SMALL": 26, "BODY": 32}
    pages: list[Image.Image] = []
    img = Image.new("RGB", (page_w, page_h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    y = margin_top
    colors = {
        "TITLE": (15, 23, 42),
        "H2": (29, 78, 216),
        "WARN": (180, 83, 9),
        "SMALL": (71, 85, 105),
        "BODY": (30, 41, 59),
    }
    for style, text in [
        (s, line) for s, raw in queue for line in _pixel_lines(raw, fonts[s], page_w - margin_x * 2)
    ]:
        step = steps[style]
        if y + step > page_h - margin_bottom:
            pages.append(img)
            img = Image.new("RGB", (page_w, page_h), (255, 255, 255))
            draw = ImageDraw.Draw(img)
            y = margin_top
        if style == "WARN":
            draw.rectangle(
                [margin_x - 8, y - 4, page_w - margin_x, y + step - 4], fill=(254, 243, 199)
            )
        draw.text((margin_x, y), text, font=fonts[style], fill=colors[style])
        y += step
    pages.append(img)
    for idx, page in enumerate(pages, start=1):
        footer_draw = ImageDraw.Draw(page)
        footer_draw.text(
            (margin_x, 30), _context(task, rv, dimension), font=fonts["SMALL"], fill=(71, 85, 105)
        )
        footer_draw.text((margin_x, 54), EXPORT_NOTICE, font=_load_font(12), fill=(71, 85, 105))
        footer_draw.text((margin_x, 76), DISCLAIMER, font=_load_font(12), fill=(71, 85, 105))
        footer_draw.text(
            (page_w // 2 - 60, page_h - 50),
            f"Page {idx} / {len(pages)}",
            font=fonts["SMALL"],
            fill=(148, 163, 184),
        )
    out = io.BytesIO()
    pages[0].save(out, format="PDF", resolution=150.0, save_all=True, append_images=pages[1:])
    return out.getvalue()
