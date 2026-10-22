from __future__ import annotations

import io
import textwrap
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont

from app.analysis.models import AnalysisTask, ReportVersion

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
    return (
        "<w:p><w:pPr><w:spacing w:after='120' w:line='300' w:lineRule='auto'/></w:pPr>"
        f"<w:r><w:rPr>{b_tag}<w:color w:val='{color}'/>"
        f"<w:sz w:val='{size}'/><w:szCs w:val='{size}'/></w:rPr>"
        f"<w:t xml:space='preserve'>{escape(text or '')}</w:t></w:r></w:p>"
    )


def _w_tc(text: str, bold: bool = False, shade: str | None = None) -> str:
    shd = f"<w:shd w:val='clear' w:color='auto' w:fill='{shade}'/>" if shade else ""
    return f"<w:tc><w:tcPr>{shd}</w:tcPr>{_w_p(text, bold=bold, size=19)}</w:tc>"


def _clause_ref(f: dict) -> str:
    return f"{f.get('document_title', '')} {f.get('clause_number', '')} (v{f.get('version_no', 1)})"


def build_report_docx(task: AnalysisTask, rv: ReportVersion) -> bytes:
    """生成八维度国别安全评估报告 Word (.docx)。"""
    country = COUNTRY_LABEL.get(task.country_code, task.country_code)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    parts: list[str] = [
        _w_p(f"车研数合 · {country} 汽车数据跨境安全评估八维度报告", bold=True, size=32),
        _w_p(
            f"任务:{task.title} | 版本:v{rv.version_no} | 车型:{task.vehicle_model} | {now}",
            size=19,
            color="475569",
        ),
    ]
    if rv.is_partial_sample:
        parts.append(_w_p(PARTIAL_NOTICE, bold=True, size=20, color="B45309"))
    parts.append(_w_p("一、执行摘要", bold=True, size=26, color="1D4ED8"))
    parts.extend(_w_p(ln.strip()) for ln in (rv.executive_summary or "").splitlines() if ln.strip())

    parts.append(_w_p("二、合规分析判定矩阵", bold=True, size=26, color="1D4ED8"))
    heads = ["业务事实", "适用条款", "合规义务", "风险等级", "判断依据(原文子串)"]
    rows = ["<w:tr>" + "".join(_w_tc(h, True, "EFF6FF") for h in heads) + "</w:tr>"]
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

    parts.append(_w_p("三、八维度评估章节", bold=True, size=26, color="1D4ED8"))
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

    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document_xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        f"<w:document xmlns:w='{ns}'><w:body>"
        + "".join(parts)
        + "<w:sectPr><w:pgSz w:w='11906' w:h='16838'/>"
        "<w:pgMar w:top='1440' w:right='1440' w:bottom='1440' w:left='1440'/></w:sectPr>"
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
    return buf.getvalue()


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if BUNDLED_FONT.exists():
        return ImageFont.truetype(str(BUNDLED_FONT), size=size)
    return ImageFont.load_default()


def _wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width=width) or [""]


def build_report_pdf(task: AnalysisTask, rv: ReportVersion) -> bytes:
    """生成多页 PDF(页面图片式,使用随包开源字体,不依赖系统字体)。"""
    page_w, page_h = 1240, 1754
    margin_x, margin_top, margin_bottom = 80, 90, 90
    fonts = {
        "TITLE": _load_font(28),
        "H2": _load_font(22),
        "BODY": _load_font(17),
        "SMALL": _load_font(15),
        "WARN": _load_font(15),
    }
    country = COUNTRY_LABEL.get(task.country_code, task.country_code)
    queue: list[tuple[str, str]] = [
        ("TITLE", f"车研数合 · {country} 汽车数据跨境八维度合规评估报告"),
        ("SMALL", f"任务:{task.title} | v{rv.version_no} | 车型:{task.vehicle_model}"),
    ]
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
    queue.append(("H2", "三、八维度评估章节"))
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
    for style, text in queue:
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
        ImageDraw.Draw(page).text(
            (page_w // 2 - 60, page_h - 50),
            f"Page {idx} / {len(pages)}",
            font=fonts["SMALL"],
            fill=(148, 163, 184),
        )
    out = io.BytesIO()
    pages[0].save(out, format="PDF", resolution=150.0, save_all=True, append_images=pages[1:])
    return out.getvalue()
