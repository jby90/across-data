"""Build public notices and CycloneDX inventory from the actual locked environment."""

from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_licenses import (
    SELF_PACKAGES,
    classify,
    js_packages,
    load_overrides,
    python_packages,
)

ROOT = Path(__file__).resolve().parents[1]


def main():
    overrides = load_overrides()
    rows = []
    linux = ROOT / "docs/compliance/python-linux-runtime.json"
    runtime_rows = []
    if linux.exists():
        actual = json.loads(linux.read_text(encoding="utf-8"))
        if actual["uv_lock_sha256"] != hashlib.sha256(
            (ROOT / "backend/uv.lock").read_bytes()
        ).hexdigest():
            raise RuntimeError("Linux runtime inventory does not match uv.lock; regenerate it")
        runtime_rows = [(p["name"], p["version"], p.get("license") or "UNKNOWN")
                        for p in actual["packages"]]
    for eco, records in [
        ("py", python_packages() + runtime_rows),
        ("js", js_packages(ROOT / "apps/web-user") + js_packages(ROOT / "apps/web-admin")),
    ]:
        for name, version, original in records:
            if name in SELF_PACKAGES:
                continue
            license_id = overrides.get(f"{eco}:{name}", original)
            if classify(license_id) in {"C", "UNKNOWN"}:
                raise RuntimeError(f"Unresolved license: {eco}:{name}")
            source = (f"https://pypi.org/project/{name}/{version}/" if eco == "py"
                      else f"https://www.npmjs.com/package/{name}/v/{version}")
            rows.append({"ecosystem": eco, "name": name, "version": version,
                         "license": license_id, "source": source})
    rows = sorted({(r["ecosystem"], r["name"], r["version"]): r for r in rows}.values(),
                  key=lambda r: (r["ecosystem"], r["name"], r["version"]))
    texts = {}
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        for f in dist.files or []:
            if re.search(r"(?:license|copying|notice)(?:[._-]|$)", f.name, re.IGNORECASE):
                path = Path(dist.locate_file(f))
                if path.is_file() and path.stat().st_size < 400000:
                    try:
                        texts[f"py/{name}/{f.name}"] = path.read_text(encoding="utf-8")
                    except UnicodeError:
                        pass
    for package in (ROOT / "node_modules/.pnpm").glob("*/node_modules/*"):
        for directory in ([package] if not package.name.startswith("@") else package.iterdir()):
            meta = directory / "package.json"
            if not meta.is_file():
                continue
            info = json.loads(meta.read_text(encoding="utf-8"))
            if not any(r["ecosystem"] == "js" and r["name"] == info.get("name")
                       and r["version"] == info.get("version") for r in rows):
                continue
            for file in directory.iterdir():
                if file.is_file() and re.match(r"license|copying|notice", file.name, re.IGNORECASE):
                    texts[f"js/{info['name']}/{info['version']}/{file.name}"] = (
                        file.read_text(encoding="utf-8", errors="replace"))
    font_manifest = json.loads((ROOT / "backend/app/analysis/fonts/open-fonts-manifest.json")
                               .read_text(encoding="utf-8"))
    for item in font_manifest["files"]:
        if item["file"].endswith("OFL.txt"):
            texts[f"fonts/{item['file']}"] = (ROOT / "backend/app/analysis/fonts" / item["file"]
                                             ).read_text(encoding="utf-8")
    content = {"scope": "锁定开发环境及生产直接/传递依赖；不代表所有包均进入生产镜像。"
                         "原生运行库见部署 SBOM。人工法务确认待完成。", "dependencies": rows,
               "font_provenance": font_manifest,
               "notice_files": {
                   k: hashlib.sha256(v.encode()).hexdigest() for k, v in texts.items()}}
    notices = "第三方开源许可声明（未修改组件；人工法务确认待完成）\n\n" + "\n\n".join(
        f"===== {key} =====\n{value}" for key, value in sorted(texts.items()))
    for app in ("web-user", "web-admin"):
        dest = ROOT / "apps" / app / "public"
        dest.mkdir(exist_ok=True)
        (dest / "licenses.json").write_text(json.dumps(content, ensure_ascii=False, indent=2)
                                            + "\n", encoding="utf-8")
        (dest / "THIRD-PARTY-NOTICES.txt").write_text(notices, encoding="utf-8")
    bom = {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
           "components": [{"type": "library", "name": r["name"], "version": r["version"],
                           "licenses": [{"license": {"name": r["license"]}}],
                           "externalReferences": [{"type": "distribution", "url": r["source"]}]}
                          for r in rows]}
    (ROOT / "docs/compliance/sbom-lock.json").write_text(json.dumps(bom, indent=2) + "\n",
                                                       encoding="utf-8")
    print(f"Generated {len(rows)} dependencies and {len(texts)} license notices.")


if __name__ == "__main__":
    main()
