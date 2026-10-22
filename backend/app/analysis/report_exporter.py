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


def _w_p(text: str, bold: bool = False, size_half_pt: int = 22, color: str = "1E293B") -> str:
    safe = escape(text or "")
    b_tag = "<w:b/>" if bold else ""
    return (
        "<w:p>"
        "<w:pPr><w:spacing w:after='120' w:line='300' w:lineRule='auto'/></w:pPr>"
        f"<w:r><w:rPr>{b_tag}<w:color w:val='{color}'/>"
        f"<w:sz w:val='{size_half_pt}'/><w:szCs w:val='{size_half_pt}'/></w:rPr>"
        f"<w:t xml:space='preserve'>{safe}</w:t></w:r>"
        "</w:p>"
    )


def _w_tc(text: str, bold: bool = False, shade: str | None = None) -> str:
    shd = f"<w:shd w:val='clear' w:color='auto' w:fill='{shade}'/>" if shade else ""
    return (
        f"<w:tc><w:tcPr>{shd}<w:tcMar>"
        "<w:top w:w='80' w:type='dxa'/><w:bottom w:w='80' w:type='dxa'/>"
        "<w:left w:w='100' w:type='dxa'/><w:right w:w='100' w:type='dxa'/>"
        f"</w:tcMar></w:tcPr>{_w_p(text, bold=bold, size_half_pt=19)}</w:tc>"
    )


def build_report_docx(task: AnalysisTask, rv: ReportVersion) -> bytes:
    """生成八维度国别安全评估报告 Word (.docx) 二进制流。"""
    body_parts: list[str] = []
    country_zh = COUNTRY_LABEL.get(task.country_code, task.country_code)

    body_parts.append(
        _w_p(
            f"车研数合 · {country_zh} 汽车数据跨境安全评估八维度报告",
            bold=True,
            size_half_pt=32,
            color="0F172A",
        )
    )
    body_parts.append(
        _w_p(
            f"任务名称：{task.title}  |  报告版本：v{rv.version_no}  |  车型：{task.vehicle_model}  |  生成时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
            bold=False,
            size_half_pt=19,
            color="475569",
        )
    )

    if rv.is_partial_sample:
        body_parts.append(
            _w_p(
                "【重要声明 · 部分样稿】当前国别部分外文法规/OCR 条款处于待律所人工复核状态(SOURCE_EVIDENCE_PENDING)，本报告仅展示已归档法源事实与核查缺口，不冒充已审定完整法律意见。",
                bold=True,
                size_half_pt=20,
                color="B45309",
            )
        )

    body_parts.append(
        _w_p("一、执行摘要 (Executive Summary)", bold=True, size_half_pt=26, color="1D4ED8")
    )
    for line in (rv.executive_summary or "").splitlines():
        if line.strip():
            body_parts.append(_w_p(line.strip(), size_half_pt=21))

    # 合规分析矩阵表
    body_parts.append(
        _w_p(
            "二、合规分析判定矩阵（业务事实 — 适用条款 — 合规义务 — 风险等级 — 判断依据）",
            bold=True,
            size_half_pt=26,
            color="1D4ED8",
        )
    )
    rows_xml: list[str] = []
    headers = ["业务事实", "适用条款", "合规义务", "风险等级", "判断依据(原文子串)"]
    rows_xml.append(
        "<w:tr>" + "".join(_w_tc(h, bold=True, shade="EFF6FF") for h in headers) + "</w:tr>"
    )
    for f in task.findings_json or []:
        clause_ref = f"{f.get('document_title', '')} {f.get('clause_number', '')} (v{f.get('version_no', 1)})"
        rows_xml.append(
            "<w:tr>"
            + _w_tc(str(f.get("business_fact", "")))
            + _w_tc(clause_ref)
            + _w_tc(str(f.get("compliance_obligation", "")))
            + _w_tc(str(f.get("risk_level", "")), bold=True)
            + _w_tc(f"{f.get('basis_quote', '')}\n【中译】{f.get('basis_zh', '')}")
            + "</w:tr>"
        )
    body_parts.append(
        "<w:tbl>"
        "<w:tblPr><w:tblW w:w='5000' w:type='pct'/>"
        "<w:tblBorders>"
        "<w:top w:val='single' w:sz='4' w:space='0' w:color='CBD5E1'/>"
        "<w:bottom w:val='single' w:sz='4' w:space='0' w:color='CBD5E1'/>"
        "<w:left w:val='single' w:sz='4' w:space='0' w:color='CBD5E1'/>"
        "<w:right w:val='single' w:sz='4' w:space='0' w:color='CBD5E1'/>"
        "<w:insideH w:val='single' w:sz='4' w:space='0' w:color='CBD5E1'/>"
        "<w:insideV w:val='single' w:sz='4' w:space='0' w:color='CBD5E1'/>"
        "</w:tblBorders></w:tblPr>" + "".join(rows_xml) + "</w:tbl>"
    )

    # 八个维度正文
    body_parts.append(
        _w_p("三、八维度国别合规深度评估章节", bold=True, size_half_pt=26, color="1D4ED8")
    )
    for dim in rv.dimensions_json or []:
        dim_title = (
            f"【{dim.get('dimension_code', '')}】{dim.get('dimension_title', '')} "
            f"（风险等级：{dim.get('risk_level', 'MEDIUM')} | 状态：{dim.get('status', 'COMPLETE')}）"
        )
        body_parts.append(_w_p(dim_title, bold=True, size_half_pt=24, color="0F172A"))
        if dim.get("missing_gap_note"):
            body_parts.append(
                _w_p(
                    f"核查缺口说明：{dim['missing_gap_note']}",
                    bold=True,
                    size_half_pt=20,
                    color="B45309",
                )
            )
        for line in str(dim.get("content_markdown", "")).splitlines():
            clean = line.strip().lstrip("#").strip()
            if clean:
                body_parts.append(_w_p(clean, size_half_pt=21))
        for cite in dim.get("citations", []) or []:
            cite_line = (
                f"  • 引用角标 {cite.get('citation_tag', '')}：{cite.get('document_title', '')} "
                f"{cite.get('clause_number', '')} (版本 v{cite.get('version_no', 1)}) — "
                f'原文："{cite.get("quote_original", "")}" | 译文：{cite.get("quote_zh", "")}'
            )
            body_parts.append(_w_p(cite_line, size_half_pt=18, color="334155"))

    document_xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
        "<w:body>" + "".join(body_parts) + "<w:sectPr><w:pgSz w:w='11906' w:h='16838'/>"
        "<w:pgMar w:top='1440' w:right='1440' w:bottom='1440' w:left='1440'/></w:sectPr>"
        "</w:body></w:document>"
    )

    content_types_xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
        "<Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/>"
        "<Default Extension='xml' ContentType='application/xml'/>"
        "<Override PartName='/word/document.xml' "
        "ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/>"
        "</Types>"
    )
    rels_xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
        "<Relationship Id='rId1' "
        "Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' "
        "Target='word/document.xml'/>"
        "</Relationships>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _load_cjk_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def build_report_pdf(task: AnalysisTask, rv: ReportVersion) -> bytes:
    """生成八维度国别安全评估报告多页 PDF (.pdf) 二进制流。"""
    page_w, page_h = 1240, 1754  # A4 @ 150 DPI
    margin_x, margin_top, margin_bottom = 80, 90, 90
    font_title = _load_cjk_font(28)
    font_h2 = _load_cjk_font(22)
    font_body = _load_cjk_font(17)
    font_small = _load_cjk_font(15)

    lines_queue: list[tuple[str, str]] = []
    country_zh = COUNTRY_LABEL.get(task.country_code, task.country_code)
    lines_queue.append(("TITLE", f"车研数合 · {country_zh} 汽车数据跨境八维度合规评估报告"))
    lines_queue.append(
        (
            "META",
            f"任务：{task.title} | 版本：v{rv.version_no} | 车型：{task.vehicle_model} | 状态：{rv.status}",
        )
    )
    if rv.is_partial_sample:
        lines_queue.append(
            (
                "WARN",
                "【部分样稿声明】当前国别部分外文条款处于待人工复核状态(SOURCE_EVIDENCE_PENDING)，仅记录已归档事实与缺口。",
            )
        )
    lines_queue.append(("H2", "一、执行摘要"))
    for raw in (rv.executive_summary or "").splitlines():
        if raw.strip():
            for wrapped in textwrap.wrap(raw.strip(), width=54):
                lines_queue.append(("BODY", wrapped))

    lines_queue.append(("H2", "二、合规分析判定矩阵"))
    for idx, f in enumerate(task.findings_json or [], start=1):
        head = (
            f"[{idx}] 事实：{f.get('business_fact', '')} -> 适用条款：{f.get('document_title', '')} "
            f"{f.get('clause_number', '')} (风险：{f.get('risk_level', '')})"
        )
        for wrapped in textwrap.wrap(head, width=54):
            lines_queue.append(("BODY_BOLD", wrapped))
        obl = (
            f"    合规义务：{f.get('compliance_obligation', '')} | 依据：{f.get('basis_quote', '')}"
        )
        for wrapped in textwrap.wrap(obl, width=54):
            lines_queue.append(("SMALL", wrapped))

    lines_queue.append(("H2", "三、八维度深度评估章节 (D1 - D8)"))
    for dim in rv.dimensions_json or []:
        lines_queue.append(
            (
                "H2",
                f"【{dim.get('dimension_code', '')}】{dim.get('dimension_title', '')} [{dim.get('risk_level', '')}]",
            )
        )
        if dim.get("missing_gap_note"):
            for wrapped in textwrap.wrap(f"核查缺口：{dim['missing_gap_note']}", width=52):
                lines_queue.append(("WARN", wrapped))
        for raw in str(dim.get("content_markdown", "")).splitlines():
            clean = raw.strip().lstrip("#").strip()
            if not clean:
                continue
            for wrapped in textwrap.wrap(clean, width=54):
                lines_queue.append(("BODY", wrapped))
        for cite in dim.get("citations", []) or []:
            c_txt = (
                f"  引用 {cite.get('citation_tag', '')}: {cite.get('document_title', '')} "
                f'{cite.get("clause_number", "")} - "{cite.get("quote_original", "")}"'
            )
            for wrapped in textwrap.wrap(c_txt, width=56):
                lines_queue.append(("SMALL", wrapped))

    pages: list[Image.Image] = []
    cur_img = Image.new("RGB", (page_w, page_h), color=(255, 255, 255))
    draw = ImageDraw.Draw(cur_img)
    y = margin_top

    def _new_page() -> tuple[Image.Image, ImageDraw.ImageDraw, int]:
        pages.append(cur_img)
        nxt = Image.new("RGB", (page_w, page_h), color=(255, 255, 255))
        return nxt, ImageDraw.Draw(nxt), margin_top

    for style, text in lines_queue:
        step = 32
        if style == "TITLE":
            step = 48
        elif style == "H2":
            step = 42
        elif style == "WARN":
            step = 34
        elif style == "SMALL":
            step = 26

        if y + step > page_h - margin_bottom:
            cur_img, draw, y = _new_page()

        if style == "TITLE":
            draw.text((margin_x, y), text, font=font_title, fill=(15, 23, 42))
        elif style == "META":
            draw.text((margin_x, y), text, font=font_small, fill=(100, 116, 139))
        elif style == "WARN":
            draw.rectangle(
                [margin_x - 8, y - 4, page_w - margin_x, y + step - 4],
                fill=(254, 243, 199),
            )
            draw.text((margin_x, y), text, font=font_small, fill=(180, 83, 9))
        elif style == "H2":
            y += 8
            draw.text((margin_x, y), text, font=font_h2, fill=(29, 78, 216))
        elif style == "BODY_BOLD":
            draw.text((margin_x, y), text, font=font_body, fill=(15, 23, 42))
        elif style == "SMALL":
            draw.text((margin_x, y), text, font=font_small, fill=(71, 85, 105))
        else:
            draw.text((margin_x, y), text, font=font_body, fill=(30, 41, 59))
        y += step

    pages.append(cur_img)
    for idx, p in enumerate(pages, start=1):
        d = ImageDraw.Draw(p)
        d.text(
            (page_w // 2 - 60, page_h - 50),
            f"Page {idx} / {len(pages)} · 车研数合",
            font=font_small,
            fill=(148, 163, 184),
        )

    out_buf = io.BytesIO()
    first, rest = pages[0], pages[1:]
    first.save(out_buf, format="PDF", resolution=150.0, save_all=True, append_images=rest)
    return out_buf.getvalue()
