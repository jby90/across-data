#!/usr/bin/env python3
"""依赖许可证门禁。

检查当前 Python 环境(以及可选的 JS 依赖清单)里的许可证,命中 C 级(默认禁止)则退出码为 1。
许可证为"未知"也视为失败,必须人工确认后在 docs/compliance/license_overrides.json 登记。

用法(在 backend 目录):
  uv run python ../scripts/check_licenses.py
  uv run python ../scripts/check_licenses.py --js ../apps/web-user ../apps/web-admin
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERRIDES = ROOT / "docs" / "compliance" / "license_overrides.json"

# C 级:默认禁止(匹配 SPDX 标识或常见写法的子串,不区分大小写)
FORBIDDEN = [
    r"\bAGPL", r"\bGPL", r"\bSSPL", r"\bBSL\b", r"Business Source", r"Elastic License",
    r"RSAL", r"Commons Clause", r"CC-BY-NC", r"CC-BY-SA", r"Non-?Commercial",
]
# LGPL 是 B 级(有条件),不能被 GPL 规则误伤
LGPL = re.compile(r"\bLGPL|Lesser General Public", re.I)
# A/B 级白名单关键字(只要命中其一,且不命中 C 级,即通过;B 级条目要求已登记)
ALLOWED = [
    "MIT", "BSD", "Apache", "ISC", "PSF", "Python Software Foundation", "PostgreSQL",
    "Unlicense", "CC0", "Zlib", "MPL", "Mozilla Public", "LGPL", "Lesser General Public",
    "OFL", "Open Font", "HPND", "0BSD", "Public Domain", "CC-BY-4.0",
]
CONDITIONAL = [r"MPL", r"Mozilla Public", r"LGPL", r"Lesser General Public", r"CC-BY-4.0"]


SELF_PACKAGES = {"cyshuhe-backend", "@cyshuhe/shared", "@cyshuhe/web-user", "@cyshuhe/web-admin"}


def load_overrides() -> dict[str, str]:
    """读取人工核对结果。格式:{"py:包名": {"license": "...", "approved_by": "...", "note": "..."}}。
    必须写明核对依据与审批人,缺一不可。"""
    if not OVERRIDES.exists():
        return {}
    raw = json.loads(OVERRIDES.read_text(encoding="utf-8"))
    result = {}
    for key, item in raw.items():
        if not item.get("license") or not item.get("approved_by") or not item.get("note"):
            raise SystemExit(f"license_overrides.json 中 {key} 缺少 license/approved_by/note")
        result[key] = item["license"]
    return result


def classify(license_text: str) -> str:
    """返回 A / B / C / UNKNOWN。"""
    text = license_text
    is_lgpl = bool(LGPL.search(text))
    scrub = LGPL.sub("", text) if is_lgpl else text
    if any(re.search(p, scrub, re.I) for p in FORBIDDEN):
        return "C"
    if any(re.search(p, text, re.I) for p in CONDITIONAL):
        return "B"
    if any(a.lower() in text.lower() for a in ALLOWED):
        return "A"
    return "UNKNOWN"


def python_packages() -> list[tuple[str, str, str]]:
    out = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--format=json", "--with-system"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [(p["Name"], p["Version"], p["License"]) for p in json.loads(out)]


def js_packages(app_dir: Path) -> list[tuple[str, str, str]]:
    out = subprocess.run(
        ["npx", "--yes", "license-checker-rseidelsohn", "--json", "--production",
         "--start", str(app_dir)],
        capture_output=True, text=True, check=True, shell=True,
    ).stdout
    data = json.loads(out)
    rows = []
    for key, meta in data.items():
        name, _, version = key.rpartition("@")
        lic = meta.get("licenses", "UNKNOWN")
        rows.append((name, version, lic if isinstance(lic, str) else " OR ".join(lic)))
    return rows


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--js", nargs="*", default=[], help="需要检查的前端应用目录")
    args = ap.parse_args()

    overrides = load_overrides()
    rows = [("py", *r) for r in python_packages()]
    for d in args.js:
        rows += [("js", *r) for r in js_packages(Path(d))]

    # 开发/测试专用包不进入生产镜像,但仍要登记许可证
    failures, conditional = [], []
    for eco, name, version, lic in sorted(rows):
        if name in SELF_PACKAGES:
            continue
        key = f"{eco}:{name}"
        effective = overrides.get(key, lic)
        level = classify(effective)
        if level in ("C", "UNKNOWN"):
            failures.append((key, version, lic, level))
        elif level == "B":
            conditional.append((key, version, lic))

    print(f"检查了 {len(rows)} 个依赖。")
    if conditional:
        print("\n[B 级:有条件,请确认已在 THIRD_PARTY.md 登记并满足条件]")
        for k, v, lic in conditional:
            print(f"  {k} {v}: {lic}")
    if failures:
        print("\n[未通过:C 级或许可证未知]")
        for k, v, lic, level in failures:
            print(f"  {k} {v}: {lic} -> {level}")
        print("\n如经法务确认可用,请在 docs/compliance/license_overrides.json 登记(键为 'py:包名'),"
              "并写明审批人。")
        return 1
    print("\n许可证检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
