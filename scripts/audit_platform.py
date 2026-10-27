#!/usr/bin/env python3
"""平台数据与质量基线审计脚本 (S0)。

仅通过 httpx 调用管理端 HTTP API(不直连数据库),统计并输出:
1. 各国采集站点数、启用数、条款确认状态、最近运行状态;
2. 各国法规文档数、版本数、条款数;
3. 译文:按国家的 engine、model_name、review_status 分布;
   CJK 字符占比 >= 30% 的比例;译文与原文完全相同的数量;
4. 图谱:按国家的节点类型数、关系类型数、review_status 分布、extractor_model 分布、未挂图谱条款数;
5. 分析任务:状态分布、backtrack_count 分布、partial_sample 比例。

用法:
  python scripts/audit_platform.py --json docs/architecture/baseline_2026-10-22.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEV_CREDS_PATH = Path.home() / ".cyshuhe-dev" / "credentials.json"
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
WS_RE = re.compile(r"\s+")
CORE_COUNTRIES = ["SG", "EG", "TN", "MA", "LB"]


def _cjk_ratio(text: str) -> float:
    compact = WS_RE.sub("", text or "")
    if not compact:
        return 0.0
    return len(CJK_RE.findall(compact)) / len(compact)


def _login_admin(client: httpx.Client, email: str, password: str) -> str:
    resp = client.post("/api/auth/admin/login", json={"email": email, "password": password})
    resp.raise_for_status()
    return resp.json()["access_token"]


def _fetch_all_documents(client: httpx.Client, headers: dict[str, str]) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    offset = 0
    limit = 200
    while True:
        r = client.get(
            "/api/admin/documents",
            headers=headers,
            params={"limit": limit, "offset": offset},
        )
        r.raise_for_status()
        batch = r.json()
        docs.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return docs


def _fetch_translations_for_country(
    client: httpx.Client,
    headers: dict[str, str],
    country_code: str,
    country_docs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    r = client.get(
        "/api/admin/knowledge/translations",
        headers=headers,
        params={"country_code": country_code, "limit": 500},
    )
    r.raise_for_status()
    items = r.json()
    if len(items) < 500 or not country_docs:
        return items

    # 若单国译文达到单页上限 500,按 document_id 逐份拉取去重
    by_id: dict[str, dict[str, Any]] = {x["id"]: x for x in items}
    for d in country_docs:
        dr = client.get(
            "/api/admin/knowledge/translations",
            headers=headers,
            params={"document_id": d["id"], "limit": 500},
        )
        dr.raise_for_status()
        for x in dr.json():
            by_id[x["id"]] = x
    return list(by_id.values())


def run_audit(base_url: str, admin_email: str, admin_password: str) -> dict[str, Any]:
    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        token = _login_admin(client, admin_email, admin_password)
        headers = {"Authorization": f"Bearer {token}"}

        # 1. 站点统计
        sites_resp = client.get("/api/admin/sites", headers=headers)
        sites_resp.raise_for_status()
        sites: list[dict[str, Any]] = sites_resp.json()

        sites_by_country: dict[str, dict[str, Any]] = {}
        for s in sites:
            cc = s["country_code"]
            bucket = sites_by_country.setdefault(
                cc,
                {
                    "total_sites": 0,
                    "enabled_sites": 0,
                    "terms_status": Counter(),
                    "last_status": Counter(),
                },
            )
            bucket["total_sites"] += 1
            if s.get("enabled"):
                bucket["enabled_sites"] += 1
            bucket["terms_status"][s.get("terms_status") or "unknown"] += 1
            bucket["last_status"][s.get("last_status") or "never_run"] += 1

        sites_summary = {
            cc: {
                "total_sites": b["total_sites"],
                "enabled_sites": b["enabled_sites"],
                "terms_status": dict(sorted(b["terms_status"].items())),
                "last_status": dict(sorted(b["last_status"].items())),
            }
            for cc, b in sorted(sites_by_country.items())
        }

        # 2. 文档、版本、条款统计
        docs = _fetch_all_documents(client, headers)
        docs_by_country: dict[str, list[dict[str, Any]]] = {}
        for d in docs:
            docs_by_country.setdefault(d["country_code"], []).append(d)

        all_countries = sorted(set(CORE_COUNTRIES) | set(docs_by_country) | set(sites_by_country))
        documents_summary: dict[str, dict[str, Any]] = {}
        total_versions = 0
        total_clauses = 0

        for cc in all_countries:
            c_docs = docs_by_country.get(cc, [])
            ver_count = 0
            clause_count = 0
            ocr_ver_count = 0
            missing_ver_count = 0
            rel_counter: Counter[str] = Counter()
            for d in c_docs:
                rel_counter[d.get("relevance") or "relevant"] += 1
                det_r = client.get(f"/api/admin/documents/{d['id']}", headers=headers)
                det_r.raise_for_status()
                det = det_r.json()
                versions = det.get("versions") or []
                ver_count += len(versions)
                ocr_ver_count += sum(
                    1 for v in versions if v.get("text_source") == "ocr" or v.get("ocr_required")
                )
                latest = det.get("latest_version")
                if not latest:
                    missing_ver_count += 1
                else:
                    c_offset = 0
                    while True:
                        cr = client.get(
                            f"/api/admin/versions/{latest['id']}/clauses",
                            headers=headers,
                            params={"limit": 500, "offset": c_offset},
                        )
                        cr.raise_for_status()
                        c_batch = cr.json()
                        clause_count += len(c_batch)
                        if len(c_batch) < 500:
                            break
                        c_offset += 500
            total_versions += ver_count
            total_clauses += clause_count
            documents_summary[cc] = {
                "documents_count": len(c_docs),
                "versions_count": ver_count,
                "clauses_count": clause_count,
                "ocr_versions_count": ocr_ver_count,
                "missing_latest_version_count": missing_ver_count,
                "relevance": dict(sorted(rel_counter.items())),
            }

        # 3. 译文统计
        translations_by_country: dict[str, dict[str, Any]] = {}
        overall_engine: Counter[str] = Counter()
        overall_model: Counter[str] = Counter()
        overall_review: Counter[str] = Counter()
        overall_tr_total = 0
        overall_cjk_ge30 = 0
        overall_identical = 0

        for cc in CORE_COUNTRIES:
            trs = _fetch_translations_for_country(
                client, headers, cc, docs_by_country.get(cc, [])
            )
            eng_c: Counter[str] = Counter()
            mod_c: Counter[str] = Counter()
            rev_c: Counter[str] = Counter()
            cjk_ok = 0
            identical = 0
            for t in trs:
                eng = t.get("engine") or "unknown"
                mod = t.get("model_name") or "unknown"
                rev = t.get("review_status") or "unknown"
                eng_c[eng] += 1
                mod_c[mod] += 1
                rev_c[rev] += 1
                overall_engine[eng] += 1
                overall_model[mod] += 1
                overall_review[rev] += 1

                orig = (t.get("original_text") or "").strip()
                trans = (t.get("translated_text") or "").strip()
                if orig and trans == orig:
                    identical += 1
                    overall_identical += 1
                if _cjk_ratio(trans) >= 0.30:
                    cjk_ok += 1
                    overall_cjk_ge30 += 1

            n_tr = len(trs)
            overall_tr_total += n_tr
            translations_by_country[cc] = {
                "total": n_tr,
                "engine": dict(sorted(eng_c.items())),
                "model_name": dict(sorted(mod_c.items())),
                "review_status": dict(sorted(rev_c.items())),
                "cjk_ratio_ge_30pct_count": cjk_ok,
                "cjk_ratio_ge_30pct_rate": round(cjk_ok / n_tr, 4) if n_tr else 0.0,
                "identical_to_original_count": identical,
            }

        translations_summary = {
            "total_translations": overall_tr_total,
            "engine": dict(sorted(overall_engine.items())),
            "model_name": dict(sorted(overall_model.items())),
            "review_status": dict(sorted(overall_review.items())),
            "cjk_ratio_ge_30pct_count": overall_cjk_ge30,
            "cjk_ratio_ge_30pct_rate": (
                round(overall_cjk_ge30 / overall_tr_total, 4) if overall_tr_total else 0.0
            ),
            "identical_to_original_count": overall_identical,
            "by_country": translations_by_country,
        }

        # 4. 知识图谱统计
        graph_by_country: dict[str, dict[str, Any]] = {}
        overall_edges = 0
        overall_nodes = 0
        overall_structural = 0
        overall_semantic = 0
        overall_unjudged = 0
        overall_low_conf = 0
        overall_quote_mismatch = 0
        overall_superseded = 0
        overall_edge_review: Counter[str] = Counter()
        overall_extractors: Counter[str] = Counter()

        for cc in CORE_COUNTRIES:
            gr_r = client.get(
                "/api/admin/knowledge/graph",
                headers=headers,
                params={"country_code": cc},
            )
            gr_r.raise_for_status()
            gr = gr_r.json()
            edges = gr.get("edges") or []
            nodes = gr.get("nodes") or []
            qs = gr.get("quality_summary") or {}
            rev_c = Counter(e.get("review_status") or "unknown" for e in edges)
            ext_c = Counter(e.get("extractor_model") or "unknown" for e in edges)
            struct_cnt = int(
                qs.get("structural_edges", sum(1 for e in edges if e.get("is_structural")))
            )
            sem_cnt = int(
                qs.get("semantic_edges", sum(1 for e in edges if not e.get("is_structural")))
            )
            unjudged_cnt = int(qs.get("semantic_unjudged", 0))
            low_conf_cnt = int(qs.get("low_confidence", 0))
            qm_cnt = int(qs.get("quote_mismatch", qs.get("quote_mismatch_edges_count", 0)))
            sup_cnt = int(qs.get("superseded_edges_count", 0))

            overall_edges += len(edges)
            overall_nodes += len(nodes)
            overall_structural += struct_cnt
            overall_semantic += sem_cnt
            overall_unjudged += unjudged_cnt
            overall_low_conf += low_conf_cnt
            overall_quote_mismatch += qm_cnt
            overall_superseded += sup_cnt
            overall_edge_review.update(rev_c)
            overall_extractors.update(ext_c)

            graph_by_country[cc] = {
                "nodes_count": len(nodes),
                "edges_count": len(edges),
                "structural_edges": struct_cnt,
                "semantic_edges": sem_cnt,
                "semantic_unjudged": unjudged_cnt,
                "low_confidence": low_conf_cnt,
                "quote_mismatch": qm_cnt,
                "superseded_edges_count": sup_cnt,
                "node_counts_by_type": gr.get("node_counts_by_type") or {},
                "edge_counts_by_relation": gr.get("edge_counts_by_relation") or {},
                "review_status": dict(sorted(rev_c.items())),
                "extractor_model": dict(sorted(ext_c.items())),
                "total_clauses_count": qs.get("total_clauses_count", 0),
                "linked_clauses_count": qs.get("linked_clauses_count", 0),
                "unlinked_clauses_count": qs.get("unlinked_clauses_count", 0),
                "quote_mismatch_edges_count": qm_cnt,
            }

        graph_summary = {
            "total_nodes": overall_nodes,
            "total_edges": overall_edges,
            "structural_edges": overall_structural,
            "semantic_edges": overall_semantic,
            "semantic_unjudged": overall_unjudged,
            "low_confidence": overall_low_conf,
            "quote_mismatch": overall_quote_mismatch,
            "superseded_edges_count": overall_superseded,
            "sg_edges_count": graph_by_country.get("SG", {}).get("edges_count", 0),
            "review_status": dict(sorted(overall_edge_review.items())),
            "extractor_model": dict(sorted(overall_extractors.items())),
            "by_country": graph_by_country,
        }

        # 5. 分析任务统计
        tasks_r = client.get("/api/admin/agent-studio/tasks", headers=headers)
        tasks_r.raise_for_status()
        tasks: list[dict[str, Any]] = tasks_r.json()

        task_status_c = Counter(t.get("status") or "unknown" for t in tasks)
        backtrack_c = Counter(str(t.get("backtrack_count", 0)) for t in tasks)
        partial_count = sum(
            1
            for t in tasks
            if t.get("is_partial_sample") or t.get("status") == "partial_sample"
        )
        finished_tasks = [
            t for t in tasks if t.get("status") in ("completed", "partial_sample")
        ]
        tasks_summary = {
            "total_tasks": len(tasks),
            "finished_tasks_count": len(finished_tasks),
            "status_distribution": dict(sorted(task_status_c.items())),
            "backtrack_count_distribution": dict(sorted(backtrack_c.items())),
            "partial_sample_count": partial_count,
            "partial_sample_rate_all": round(partial_count / len(tasks), 4) if tasks else 0.0,
            "partial_sample_rate_finished": (
                round(partial_count / len(finished_tasks), 4) if finished_tasks else 0.0
            ),
        }

        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "base_url": base_url,
            "sites": {
                "total_sites": len(sites),
                "by_country": sites_summary,
            },
            "documents": {
                "total_documents": len(docs),
                "total_versions": total_versions,
                "total_clauses": total_clauses,
                "by_country": documents_summary,
            },
            "translations": translations_summary,
            "knowledge_graph": graph_summary,
            "analysis_tasks": tasks_summary,
        }


def print_human_report(report: dict[str, Any]) -> None:
    print("=" * 72)
    print(f"车研数合 · 平台数据与质量基线审计报告 ({report['generated_at']})")
    print("=" * 72)

    print("\n[1] 各国采集站点统计:")
    for cc, info in report["sites"]["by_country"].items():
        print(
            f"  - {cc}: 总计 {info['total_sites']} 个 | 启用 {info['enabled_sites']} 个 | "
            f"条款状态 {info['terms_status']} | 最近运行 {info['last_status']}"
        )

    docs_meta = report["documents"]
    print(
        f"\n[2] 各国法规文档/版本/条款统计 (总文档 {docs_meta['total_documents']} 份, "
        f"总版本 {docs_meta['total_versions']} 个, 总条款 {docs_meta['total_clauses']} 条):"
    )
    for cc, info in docs_meta["by_country"].items():
        ocr_cnt = info.get("ocr_versions_count", 0)
        miss_cnt = info.get("missing_latest_version_count", 0)
        print(
            f"  - {cc}: 文档 {info['documents_count']} 份 | 版本 {info['versions_count']} 个 "
            f"(OCR {ocr_cnt} 个, 缺版本 {miss_cnt} 份) | "
            f"条款 {info['clauses_count']} 条 | 相关性 {info.get('relevance', {})}"
        )

    tr = report["translations"]
    print(f"\n[3] 机器翻译与复核统计 (总译文 {tr['total_translations']} 条):")
    print(f"  - 引擎分布 (engine): {tr['engine']}")
    print(f"  - 模型分布 (model_name): {tr['model_name']}")
    print(f"  - 复核状态 (review_status): {tr['review_status']}")
    print(
        f"  - CJK 字符占比 >= 30%: {tr['cjk_ratio_ge_30pct_count']}/{tr['total_translations']} "
        f"({tr['cjk_ratio_ge_30pct_rate'] * 100:.1f}%)"
    )
    print(f"  - 译文与原文完全相同数量: {tr['identical_to_original_count']} 条")
    for cc, c_tr in tr["by_country"].items():
        cjk_pct = c_tr["cjk_ratio_ge_30pct_rate"] * 100
        print(
            f"    * {cc}: {c_tr['total']} 条 | engine={c_tr['engine']} | "
            f"status={c_tr['review_status']} | CJK>=30%: {c_tr['cjk_ratio_ge_30pct_count']} "
            f"({cjk_pct:.1f}%) | 与原文相同: {c_tr['identical_to_original_count']}"
        )

    kg = report["knowledge_graph"]
    print(
        f"\n[4] 知识图谱统计 (五国总节点 {kg['total_nodes']} 个, "
        f"五国活跃总边 {kg['total_edges']} 条: "
        f"结构边 {kg.get('structural_edges', 0)} 条 + 语义边 {kg.get('semantic_edges', 0)} 条 | "
        f"已作废旧规则边 superseded={kg.get('superseded_edges_count', 0)} 条):"
    )
    print(f"  - 抽取器分布 (extractor_model): {kg['extractor_model']}")
    print(f"  - 复核状态 (review_status): {kg['review_status']}")
    print(
        f"  - 质量核验: quote_mismatch={kg.get('quote_mismatch', 0)} | "
        f"semantic_unjudged={kg.get('semantic_unjudged', 0)} | "
        f"low_confidence={kg.get('low_confidence', 0)}"
    )
    for cc, c_kg in kg["by_country"].items():
        unlinked = c_kg["unlinked_clauses_count"]
        tot_cl = c_kg["total_clauses_count"]
        sup_cnt = c_kg.get("superseded_edges_count", 0)
        print(
            f"    * {cc}: 节点 {c_kg['nodes_count']} ({c_kg['node_counts_by_type']}) | "
            f"边 {c_kg['edges_count']} (结构 {c_kg.get('structural_edges', 0)} / "
            f"语义 {c_kg.get('semantic_edges', 0)}, superseded={sup_cnt}) | "
            f"quote_mismatch={c_kg.get('quote_mismatch', 0)} | "
            f"未挂图谱条款 {unlinked}/{tot_cl} | review={c_kg['review_status']}"
        )

    tk = report["analysis_tasks"]
    print(f"\n[5] 多 Agent 分析任务统计 (总任务 {tk['total_tasks']} 条):")
    print(f"  - 状态分布: {tk['status_distribution']}")
    print(f"  - 质检退回次数分布 (backtrack_count): {tk['backtrack_count_distribution']}")
    print(
        f"  - 部分样稿 (partial_sample): {tk['partial_sample_count']} 条 "
        f"(占全部任务 {tk['partial_sample_rate_all'] * 100:.1f}%, "
        f"占已出报告任务 {tk['partial_sample_rate_finished'] * 100:.1f}%)"
    )
    print("=" * 72)


def prepare_cleanup_backup(
    base_url: str, admin_email: str, admin_password: str, backup_path: Path
) -> dict[str, Any]:
    """导出待清理的 ZZ 演示站点与 E2E 测试对话(组织='演示车企' 且标题含 'CYS-Pilot')完整备份。"""
    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        token = _login_admin(client, admin_email, admin_password)
        headers = {"Authorization": f"Bearer {token}"}

        sites = client.get("/api/admin/sites", headers=headers).json()
        zz_sites = [s for s in sites if s.get("country_code") == "ZZ"]
        zz_runs: dict[str, list[dict[str, Any]]] = {}
        zz_docs: list[dict[str, Any]] = []
        for s in zz_sites:
            runs_r = client.get(f"/api/admin/sites/{s['id']}/runs", headers=headers)
            zz_runs[s["id"]] = runs_r.json() if runs_r.status_code == 200 else []
            docs_r = client.get(
                "/api/admin/documents", headers=headers, params={"site_id": s["id"], "limit": 200}
            )
            if docs_r.status_code == 200:
                for d in docs_r.json():
                    det_r = client.get(f"/api/admin/documents/{d['id']}", headers=headers)
                    zz_docs.append(det_r.json() if det_r.status_code == 200 else d)

        tasks = client.get("/api/admin/agent-studio/tasks", headers=headers).json()
        target_tasks_full: list[dict[str, Any]] = []
        all_tasks_full: list[dict[str, Any]] = []
        for t in tasks:
            det_r = client.get(f"/api/admin/agent-studio/tasks/{t['id']}", headers=headers)
            full_obj = det_r.json() if det_r.status_code == 200 else t
            all_tasks_full.append(full_obj)
            if t.get("org_name") == "演示车企" and "CYS-Pilot" in (t.get("title") or ""):
                target_tasks_full.append(full_obj)

        backup = {
            "exported_at": datetime.now(UTC).isoformat(),
            "zz_sites": zz_sites,
            "zz_runs_by_site_id": zz_runs,
            "zz_documents": zz_docs,
            "e2e_cys_pilot_tasks": target_tasks_full,
            "all_tasks_snapshot": all_tasks_full,
        }
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_text(
            json.dumps(backup, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return backup


def execute_cleanup(
    base_url: str, admin_email: str, admin_password: str
) -> dict[str, list[str]]:
    """通过管理端 HTTP API 删除 ZZ 演示站点及 12 条脚本产生的测试对话(保留吉利L4手动对话)。"""
    deleted_sites: list[str] = []
    deleted_tasks: list[str] = []
    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        token = _login_admin(client, admin_email, admin_password)
        headers = {"Authorization": f"Bearer {token}"}

        sites = client.get("/api/admin/sites", headers=headers).json()
        for s in sites:
            if s.get("country_code") == "ZZ":
                r = client.delete(f"/api/admin/sites/{s['id']}", headers=headers)
                r.raise_for_status()
                deleted_sites.append(f"{s['country_code']}:{s['id']}:{s['publisher']}")

        tasks = client.get("/api/admin/agent-studio/tasks", headers=headers).json()
        for t in tasks:
            title = t.get("title") or ""
            vmodel = t.get("vehicle_model") or ""
            is_script_task = (
                t.get("org_name") == "演示车企"
                and (
                    "CYS-Pilot" in title
                    or "CYS-Pilot" in vmodel
                    or "CYS-RoboTaxi" in vmodel
                    or (title == "新对话" and t.get("status") == "collecting_facts")
                )
            )
            if is_script_task:
                r = client.delete(
                    f"/api/admin/agent-studio/tasks/{t['id']}", headers=headers
                )
                r.raise_for_status()
                deleted_tasks.append(f"{t['conversation_code']} ({title})")

    return {"deleted_sites": deleted_sites, "deleted_tasks": deleted_tasks}


def main() -> int:
    parser = argparse.ArgumentParser(description="平台数据与质量基线审计脚本 (httpx API)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="后端服务地址")
    parser.add_argument("--email", default="admin@example.com", help="平台管理员邮箱")
    parser.add_argument(
        "--password",
        default=None,
        help="平台管理员密码(默认读取 ~/.cyshuhe-dev/credentials.json)",
    )
    parser.add_argument("--json", dest="json_out", default=None, help="输出 JSON 文件路径")
    parser.add_argument(
        "--prepare-cleanup-backup",
        dest="backup_out",
        default=None,
        help="导出待清理的 ZZ 站点与 CYS-Pilot 测试对话备份 JSON",
    )
    parser.add_argument(
        "--execute-cleanup",
        action="store_true",
        help="经用户确认后执行 ZZ 演示站点与自动化测试对话清理",
    )
    args = parser.parse_args()

    password = args.password
    if not password:
        creds = json.loads(DEV_CREDS_PATH.read_text(encoding="utf-8"))
        password = creds["users"][args.email]

    if args.backup_out:
        b_path = Path(args.backup_out)
        b_data = prepare_cleanup_backup(args.base_url, args.email, password, b_path)
        print(
            f"\n[已导出待清理数据备份]: {b_path} "
            f"(ZZ 站点 {len(b_data['zz_sites'])} 个, ZZ 文档 {len(b_data['zz_documents'])} 份, "
            f"CYS-Pilot 测试对话 {len(b_data['e2e_cys_pilot_tasks'])} 条)"
        )

    if args.execute_cleanup:
        res = execute_cleanup(args.base_url, args.email, password)
        print(f"\n[已清理 ZZ 站点]: {res['deleted_sites']}")
        print(f"[已清理测试对话 ({len(res['deleted_tasks'])} 条)]: {res['deleted_tasks']}")

    report = run_audit(args.base_url, args.email, password)
    print_human_report(report)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        out_path.write_text(payload, encoding="utf-8")
        print(f"\n[已写入 JSON 基线文件]: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
