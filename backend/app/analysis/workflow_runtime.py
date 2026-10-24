"""Executable version contract. Configuration never selects Python code or tools."""

import copy
import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.models import AgentWorkflowConfig, AnalysisTask
from app.analysis.prompts import ANALYSIS_SYSTEM_PROMPT
from app.core.config import get_settings
from app.knowledge.history import snapshot

RUNTIME_SCHEMA = "s5-grounded-rules-v2"
# Applicable fields only: deterministic nodes do not pretend to use LLM parameters.
PARAMETERS = {
    "node_start": set(),
    "node_fact_collector": set(),
    "node_mcp_retriever": {"top_k", "timeout"},
    "node_dimension_analyst": {"temperature", "timeout"},
    "node_citation_verifier": {"max_backtrack_rounds"},
    "node_report_compiler": {"timeout"},
    "node_update_linkage": {"enabled", "timeout"},
}


class NodeExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool = True
    model: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$"
    )
    temperature: float = Field(default=0.1, ge=0, le=2)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=16000)
    top_k: int = Field(default=5, ge=1, le=30)
    max_backtrack_rounds: int = Field(default=2, ge=0, le=2)
    timeout: float = Field(default=150.0, ge=1, le=300)


def baseline_nodes() -> list[dict]:
    from app.analysis.workflow_engine import DEFAULT_WORKFLOW_NODES

    nodes = copy.deepcopy(DEFAULT_WORKFLOW_NODES)
    by_id = {n["node_id"]: n for n in nodes}
    entries = {
        "node_start": "app.analysis.router:_start",
        "node_fact_collector": "app.analysis.workflow_engine:evaluate_fact_sufficiency",
        "node_mcp_retriever": "app.analysis.tools:ToolBox.search_effective_clauses",
        "node_dimension_analyst": "app.analysis.pipeline:_analyse_dimension",
        "node_citation_verifier": "app.analysis.pipeline:_verify_dimension",
        "node_report_compiler": "app.analysis.pipeline:_compile",
        "node_update_linkage": "app.analysis.pipeline:_update_linkage",
    }
    for nid, entry in entries.items():
        by_id[nid]["code_entrypoint"] = entry
    by_id["node_fact_collector"]["parameters"] = {
        "sufficiency_threshold": 100,
        "block_on_missing_facts": True,
    }
    retriever = by_id["node_mcp_retriever"]["parameters"]
    retriever.pop("top_k_clauses", None)
    retriever.pop("temperature", None)
    retriever.pop("only_effective_versions", None)
    retriever.update(
        top_k=5, timeout=150.0, exclude_held_sources=True, unknown_status_is_not_verified=True
    )
    by_id["node_mcp_retriever"]["system_prompt"] = (
        "确定性检索：限定目标国家，核对归档完整性，隔离错绑来源和待核对解析；"
        "排除明确失效版本，未知法律状态如实标注，不能视为已确认现行有效。"
    )
    analyst = by_id["node_dimension_analyst"]
    analyst["system_prompt"] = ANALYSIS_SYSTEM_PROMPT
    analyst["model_name"] = get_settings().llm_model
    analyst["parameters"].update(temperature=0.0, timeout=150.0)
    verifier = by_id["node_citation_verifier"]["parameters"]
    verifier.pop("max_backtrack_loops", None)
    verifier["max_backtrack_rounds"] = 2
    by_id["node_report_compiler"]["parameters"]["timeout"] = 150.0
    by_id["node_update_linkage"]["parameters"].update(enabled=True, timeout=150.0)
    return nodes


def execution_settings(node: dict) -> dict:
    params = {key: node["parameters"][key] for key in PARAMETERS[node["node_id"]]}
    if node["node_id"] == "node_dimension_analyst":
        if not all(
            isinstance(node.get(key), str) and node[key].strip()
            for key in ("model_name", "system_prompt")
        ):
            raise ValueError("分析节点的模型和实际提示词不能为空")
        params.update(model=node["model_name"], system_prompt=node["system_prompt"])
    return NodeExecutionSettings.model_validate(params).model_dump(include=set(params))


def validate_graph(nodes, edges) -> list[dict]:
    from app.analysis.workflow_engine import DEFAULT_WORKFLOW_EDGES

    canonical = {n["node_id"]: n for n in baseline_nodes()}
    if not isinstance(nodes, list) or len(nodes) != len(canonical):
        raise ValueError("节点拓扑不可修改；请从可执行基线导出配置")
    if (
        not isinstance(edges, list)
        or not all(isinstance(e, dict) for e in edges)
        or len(edges) != len(DEFAULT_WORKFLOW_EDGES)
        or sorted(edges, key=lambda e: str(e.get("edge_id")))
        != sorted(DEFAULT_WORKFLOW_EDGES, key=lambda e: e["edge_id"])
    ):
        raise ValueError("连接拓扑、工具绑定及流转条件不可修改")
    seen = set()
    normalized = []
    for node in nodes:
        if not isinstance(node, dict) or node.get("node_id") not in canonical:
            raise ValueError("未知或非法工作流节点")
        nid = node["node_id"]
        if nid in seen:
            raise ValueError("重复工作流节点")
        seen.add(nid)
        base = canonical[nid]
        if node.keys() != base.keys():
            raise ValueError(f"{nid}: 不允许增加或删除节点字段")
        mutable = {"parameters", "readonly_locked"}
        if nid == "node_dimension_analyst":
            mutable |= {"model_name", "system_prompt"}
        for key in base.keys() - mutable:
            if node[key] != base[key]:
                raise ValueError(f"{nid}.{key}: 代码入口、工具、拓扑及固定参数不可修改")
        params = node["parameters"]
        if not isinstance(params, dict) or params.keys() != base["parameters"].keys():
            raise ValueError(f"{nid}: 参数只能使用当前节点白名单")
        for key in params.keys() - PARAMETERS[nid]:
            if params[key] != base["parameters"][key]:
                raise ValueError(f"{nid}.{key}: 证据闸门及固定参数不可修改")
        clean = copy.deepcopy(node)
        clean["readonly_locked"] = True
        execution_settings(clean)  # Strict types/ranges; no silently ignored parameters.
        normalized.append(clean)
    return normalized


def require_executable(cfg: AgentWorkflowConfig) -> None:
    if cfg.runtime_schema_version != RUNTIME_SCHEMA:
        raise ValueError(
            "此历史版本仅供查看，未记录可执行参数；请导入并激活当前原文句子选择协议版本"
        )
    validate_graph(cfg.nodes_json, cfg.edges_json)


async def workflow_lock(session: AsyncSession):
    # Serialize bootstrap/import/activation across requests and processes.
    await session.execute(text("SELECT pg_advisory_xact_lock(64042004)"))


async def ensure_baseline(session: AsyncSession) -> AgentWorkflowConfig:
    from app.analysis.workflow_engine import DEFAULT_WORKFLOW_EDGES, WORKFLOW_KEY

    await workflow_lock(session)
    rows = list(
        (
            await session.scalars(
                select(AgentWorkflowConfig)
                .where(
                    AgentWorkflowConfig.workflow_key == WORKFLOW_KEY,
                )
                .order_by(AgentWorkflowConfig.version_no.desc())
            )
        ).all()
    )
    active = next((r for r in rows if r.is_active), None)
    current = next((r for r in rows if r.runtime_schema_version == RUNTIME_SCHEMA), None)
    if current is None:
        current = AgentWorkflowConfig(
            workflow_key=WORKFLOW_KEY,
            version_no=(rows[0].version_no + 1 if rows else 1),
            name="汽车数据跨境八维度合规评估工作流",
            description="版本固定的检索、模型分析、确定性核验与报告编制；参数由受控导入配置驱动。",
            dsl_version="cyshuhe-dify-dsl/1.0",
            runtime_schema_version=RUNTIME_SCHEMA,
            is_active=not rows,
            readonly_locked=True,
            nodes_json=baseline_nodes(),
            edges_json=copy.deepcopy(DEFAULT_WORKFLOW_EDGES),
            change_note=("[baseline-s5-quality] 问题检索与依据限定生成；旧库仅追加待激活版本"),
        )
        session.add(current)
        await session.flush()
        snapshot(session, current, "workflow_baseline_created")
    await session.commit()
    return active or current


async def import_version(session, payload, change_note, user_id):
    from app.analysis.workflow_engine import (
        EIGHT_DIMENSIONS_SPEC,
        REQUIRED_FACTS_SPEC,
        WORKFLOW_KEY,
    )

    if payload.keys() - {
        "kind",
        "schema_version",
        "runtime_schema_version",
        "exported_at",
        "security_policy",
        "app",
        "workflow",
    }:
        raise ValueError("配置包包含白名单外字段")

    workflow = payload.get("workflow")
    if not isinstance(workflow, dict):
        raise ValueError("缺少 workflow 配置对象")
    if workflow.keys() - {"graph", "report_dimensions", "required_facts_schema"}:
        raise ValueError("工作流只允许导入现有图与参数")
    dimensions = [{"code": code, "title": title} for code, title in EIGHT_DIMENSIONS_SPEC]
    if workflow.get("report_dimensions", dimensions) != dimensions:
        raise ValueError("报告维度契约不可修改")
    if workflow.get("required_facts_schema", REQUIRED_FACTS_SPEC) != REQUIRED_FACTS_SPEC:
        raise ValueError("必填事实契约不可修改")
    graph = workflow.get("graph")
    if not isinstance(graph, dict):
        raise ValueError("缺少 workflow.graph 配置")
    if graph.keys() != {"nodes", "edges"}:
        raise ValueError("graph 只能包含固定 nodes 与 edges")
    nodes = validate_graph(graph.get("nodes"), graph.get("edges"))
    await workflow_lock(session)
    latest = await session.scalar(
        select(AgentWorkflowConfig.version_no)
        .where(
            AgentWorkflowConfig.workflow_key == WORKFLOW_KEY,
        )
        .order_by(AgentWorkflowConfig.version_no.desc())
        .limit(1)
    )
    meta = payload.get("app") or {}
    if not isinstance(meta, dict):
        raise ValueError("app 元数据必须为对象")
    if meta.keys() - {"workflow_key", "name", "description", "version_no", "change_note"}:
        raise ValueError("app 元数据包含非白名单字段")
    cfg = AgentWorkflowConfig(
        workflow_key=WORKFLOW_KEY,
        version_no=(latest or 0) + 1,
        name=str(meta.get("name") or "八维度合规工作流")[:200],
        description=str(meta.get("description") or "受控配置导入"),
        dsl_version="cyshuhe-dify-dsl/1.0",
        runtime_schema_version=RUNTIME_SCHEMA,
        is_active=False,
        readonly_locked=True,
        nodes_json=nodes,
        edges_json=graph["edges"],
        change_note=change_note,
        created_by=user_id,
    )
    session.add(cfg)
    await session.flush()
    snapshot(session, cfg, "workflow_imported", user_id)
    await session.commit()
    return cfg


async def activate_version(session, version: str, user_id):
    from app.analysis.workflow_engine import WORKFLOW_KEY

    await workflow_lock(session)
    try:
        version_filter = AgentWorkflowConfig.id == uuid.UUID(version)
    except ValueError:
        if not version.isdigit():
            raise LookupError("目标工作流版本不存在") from None
        version_filter = AgentWorkflowConfig.version_no == int(version)
    target = await session.scalar(
        select(AgentWorkflowConfig).where(
            AgentWorkflowConfig.workflow_key == WORKFLOW_KEY,
            version_filter,
        )
    )
    if target is None:
        raise LookupError("目标工作流版本不存在")
    require_executable(target)
    old = await session.scalar(
        select(AgentWorkflowConfig).where(
            AgentWorkflowConfig.workflow_key == WORKFLOW_KEY,
            AgentWorkflowConfig.is_active,
        )
    )
    if old:
        snapshot(session, old, "workflow_before_activation", user_id)
    await session.execute(
        update(AgentWorkflowConfig)
        .where(
            AgentWorkflowConfig.workflow_key == WORKFLOW_KEY,
            AgentWorkflowConfig.is_active,
        )
        .values(is_active=False)
    )
    await session.flush()
    target.is_active = True
    await session.flush()
    snapshot(session, target, "workflow_activated", user_id)
    await session.commit()
    return target


async def pin_active_version(session: AsyncSession, task: AnalysisTask) -> AgentWorkflowConfig:
    cfg = await ensure_baseline(session)
    if not cfg.is_active:
        raise ValueError("没有当前激活的可执行工作流版本，请管理员先激活")
    require_executable(cfg)
    task.workflow_version_id = cfg.id
    task.workflow_version_no = cfg.version_no
    return cfg
