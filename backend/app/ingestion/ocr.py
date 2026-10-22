"""OCR:给没有文字层的扫描件 PDF 补文字(Tesseract,Apache-2.0)。

OCR 文本是机器识别结果,必然有错字、错号。因此:
- 版本记录 text_source='ocr' 和平均置信度,界面必须显著提示"需人工核对";
- OCR 文本不会冒充 PDF 文字层原文,只用于检索与初步切分。
"""

from __future__ import annotations

import logging
import os
import shutil
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import pytesseract

log = logging.getLogger(__name__)

# 文档语言 -> Tesseract 语言包。阿拉伯文/法文文件里常夹英文,带上 eng 提升混排识别。
LANG_MAP = {"en": "eng", "ar": "ara+eng", "fr": "fra+eng", "zh": "chi_sim+eng"}
LOW_CONFIDENCE = 60.0


class OcrUnavailable(Exception):
    pass


@dataclass
class OcrResult:
    pages: list[str]
    mean_confidence: float  # 0-100,按词平均
    languages: str


class OcrEngine:
    def __init__(self, cmd: str | None, tessdata_dir: str | None, dpi: int = 300) -> None:
        self._cmd = cmd or shutil.which("tesseract")
        self._tessdata = tessdata_dir
        self._dpi = dpi
        self._checked: bool | None = None

    @property
    def available(self) -> bool:
        if self._checked is None:
            self._checked = False
            if self._cmd and Path(self._cmd).exists():
                try:
                    out = subprocess.run(
                        [self._cmd, "--version"], capture_output=True, text=True, timeout=15
                    )
                    self._checked = out.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    self._checked = False
        return bool(self._checked)

    def _config(self) -> str:
        # 语言包目录通过 TESSDATA_PREFIX 传递(命令行里带路径,在 Windows 上会被 shlex 当成转义处理)
        return "--psm 3"

    def languages_for(self, language: str) -> str:
        return LANG_MAP.get(language.split("-")[0].lower(), "eng")

    def run(self, content: bytes, language: str) -> OcrResult:
        """同步执行(调用方应放到线程里)。逐页渲染后识别。"""
        if not self.available:
            raise OcrUnavailable("未找到可用的 Tesseract")
        assert self._cmd is not None
        pytesseract.pytesseract.tesseract_cmd = self._cmd
        if self._tessdata:
            os.environ["TESSDATA_PREFIX"] = self._tessdata
        langs = self.languages_for(language)
        pages: list[str] = []
        confs: list[float] = []
        doc = pdfium.PdfDocument(content)
        try:
            for i in range(len(doc)):
                image = doc[i].render(scale=self._dpi / 72).to_pil().convert("L")
                text = pytesseract.image_to_string(image, lang=langs, config=self._config())
                data = pytesseract.image_to_data(
                    image,
                    lang=langs,
                    config=self._config(),
                    output_type=pytesseract.Output.DICT,
                )
                confs.extend(
                    float(c)
                    for c, w in zip(data["conf"], data["text"], strict=False)
                    if str(w).strip() and float(c) >= 0
                )
                pages.append("\n".join(ln.rstrip() for ln in text.splitlines() if ln.strip()))
        finally:
            doc.close()
        mean = statistics.fmean(confs) if confs else 0.0
        return OcrResult(pages=pages, mean_confidence=round(mean, 1), languages=langs)
