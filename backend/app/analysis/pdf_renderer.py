"""Offline, HarfBuzz-shaped report export. No model, network or data mutation."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import unicodedata
from functools import lru_cache
from html import escape
from pathlib import Path

from app.analysis.citation_policy import policy_label, review_detail, review_label, review_overview

FONT_DIR = Path(__file__).with_name("fonts")
_RENDER_LOCK = threading.Lock()  # Bound native rendering memory on small prototype hosts.


@lru_cache(maxsize=1)
def verified_fonts():
    manifest = json.loads((FONT_DIR / "open-fonts-manifest.json").read_text(encoding="utf-8"))
    fonts = {}
    for item in manifest["files"]:
        path = (FONT_DIR / item["file"]).resolve()
        if not path.is_relative_to(FONT_DIR.resolve()):
            raise ValueError("字体路径越界")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("随包字体校验失败")
        if path.suffix == ".ttf":
            fonts[item["family"]] = path.as_uri()
    return fonts


def _text(value):
    return escape(str(value or ""), quote=True)


def _p(value, cls=""):
    # HTML dir=auto is not implemented consistently by paged-media engines.
    # Unicode's first-strong rule works for every language without country rules.
    strong = next((unicodedata.bidirectional(c) for c in str(value or "")
                   if unicodedata.bidirectional(c) in {"R", "AL", "L"}), "L")
    direction = "rtl" if strong in {"R", "AL"} else "ltr"
    return f'<p class="{cls} {direction}" dir="{direction}">{_text(value)}</p>'


def report_html(task, rv, *, dimension, context, country, notices):
    fonts = verified_fonts()
    css_fonts = "\n".join(
        f"@font-face{{font-family:'{name}';src:url('{uri}');font-weight:100 900}}"
        for name, uri in fonts.items()
    )
    parts = [
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><style>",
        css_fonts,
        """
        @page {size:A4; margin:28mm 18mm 22mm;
          @top-left {content:element(reportHeader)}
          @bottom-center {content:counter(page) ' / ' counter(pages);font-size:9pt}}
        body{font-family:inter,notosanssc,notonaskharabic,sans-serif;
          font-size:10.5pt;color:#20323f;line-height:1.5;hyphens:none}
        header{position:running(reportHeader);font-size:8pt;color:#64748b}
        h1{font-size:20pt;line-height:1.4} h2{font-size:14pt;color:#244bc3}
        h3{font-size:12pt} h1,h2,h3{break-after:avoid}
        p{margin:5pt 0;white-space:pre-wrap;overflow-wrap:anywhere}
        .rtl{direction:rtl;text-align:right;font-size:12pt}
        .notice{padding:7pt;background:#fff7e8;color:#855800}
        .meta,.citation{font-size:9pt;color:#43546a}
        .finding{border-left:2pt solid #dce4fc;padding-left:8pt;margin:10pt 0}
        .citation{border-top:0.5pt solid #e7ebef;padding-top:5pt}
        .citation-block{break-inside:avoid}
        </style><body>""",
        f"<header>{_text(context)}<br>{_text(notices[0])}</header>",
        f"<h1>{_text(country)} 汽车数据跨境合规评估报告 {_text(dimension or '八维度')}</h1>",
        _p(f"任务：{task.title} | v{rv.version_no} | 车型：{task.vehicle_model}", "meta"),
        *[_p(n, "notice") for n in notices],
        _p(review_overview(rv.dimensions_json)),
        _p(f"生成时引用策略：{policy_label(rv.citation_policy)}", "meta"),
        "<h2>一、执行摘要</h2>", _p(rv.executive_summary),
        "<h2>二、合规分析判定矩阵</h2>",
    ]
    for idx, finding in enumerate(task.findings_json or [], 1):
        parts.extend([
            '<section class="finding">',
            _p(f"[{idx}] {finding.get('business_fact', '')}"),
            _p(f"{finding.get('document_title', '')} {finding.get('clause_number', '')} "
               f"(v{finding.get('version_no', 1)}) | 风险：{finding.get('risk_level', '')}"),
            _p(f"义务：{finding.get('compliance_obligation', '')}"),
            _p(finding.get("basis_quote", ""), "citation"), "</section>",
        ])
    if not task.findings_json:
        parts.append(_p("该版本未保存判定矩阵快照，请查看本章引用；未借用当前任务数据。"))
    parts.append("<h2>三、评估章节</h2>")
    for dim in rv.dimensions_json:
        parts.append(f"<h3>{_text(dim.get('dimension_code'))} "
                     f"{_text(dim.get('dimension_title'))} / {_text(dim.get('risk_level'))}</h3>")
        if dim.get("missing_gap_note"):
            parts.append(_p("核查缺口：" + dim["missing_gap_note"], "notice"))
        parts.append(_p(dim.get("content_markdown")))
        for cite in dim.get("citations", []) or []:
            parts.extend([
                '<aside class="citation-block">',
                _p(f"引用 {cite.get('citation_tag', '')}：{cite.get('document_title', '')} "
                   f"{cite.get('clause_number', '')} (v{cite.get('version_no', 1)})", "citation"),
                _p(cite.get("source_notice"), "meta") if cite.get("source_notice") else "",
                _p(cite.get("quote_original")),
                _p(cite.get("quote_zh")) if cite.get("quote_zh") else "",
                _p(f"来源：{cite.get('source_url') or '未提供'}", "meta"),
                _p(f"人工复核：{review_label(cite)}；{review_detail(cite)}", "meta"),
                "</aside>",
            ])
    return "".join(parts) + "</body></html>"


def render_pdf(task, rv, **kwargs):
    # Lazy import: installation/runtime failures must not degrade to unshaped text.
    os.environ.setdefault("FONTCONFIG_FILE", str(FONT_DIR / "fontconfig.xml"))
    from weasyprint import HTML
    from weasyprint.text.fonts import FontConfiguration
    from weasyprint.urls import URLFetcher

    allowed = frozenset(verified_fonts().values())

    class OfflineFonts(URLFetcher):
        def fetch(self, url, headers=None):
            if url not in allowed:
                raise ValueError("报告渲染禁止网络及非字体文件访问")
            return super().fetch(url, headers)

    with _RENDER_LOCK:
        return HTML(string=report_html(task, rv, **kwargs), url_fetcher=OfflineFonts(
            allowed_protocols={"file"}, fail_on_errors=True,
        )).write_pdf(
            font_config=FontConfiguration(), full_fonts=False,
        )
