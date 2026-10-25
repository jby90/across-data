"""PostgreSQL transactional outbox + one LISTEN connection per API process/engine.

Notifications contain only an event ID. Durable rows allow replay after disconnects
or restarts. Subscribers never hold a database session while waiting for the client.
"""

import asyncio
import json

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.models import AnalysisEvent, AnalysisTask
from app.iam.models import ADMIN_ROLES, Organization, User

_hubs = {}
TERMINAL = {"completed", "partial_sample", "failed", "awaiting_user_facts"}


async def append_event(session, task, event_type, payload=None):
    await session.flush()
    # IDs must follow commit order; otherwise a slower transaction can be skipped on replay.
    await session.execute(text("SELECT pg_advisory_xact_lock(64042007)"))
    event = AnalysisEvent(
        task_id=task.id, org_id=task.org_id, event_type=event_type, payload=payload or {}
    )
    session.add(event)
    await session.flush()
    await session.execute(text("SELECT pg_notify('analysis_events', :id)"), {"id": str(event.id)})
    return event


async def trace_events(session, task, tr):
    data = {
        "trace_id": str(tr.id),
        "phase": tr.phase,
        "node_id": tr.agent_node_id,
        "status": tr.status,
        "workflow_version_id": str(task.workflow_version_id),
    }
    for tool in tr.tool_calls_json:
        await append_event(
            session,
            task,
            "tool_called",
            data
            | {
                "tool_name": tool["tool_name"],
                "duration_ms": tool["duration_ms"],
            },
        )
    if tr.tokens_used:
        await append_event(
            session,
            task,
            "token_usage",
            data
            | {
                "tokens_used": tr.tokens_used,
                "total_tokens": task.total_tokens,
            },
        )
    await append_event(session, task, "phase_finished", data)
    if tr.phase == "user_fact_question":
        await append_event(session, task, "awaiting_user_facts", data)
    elif tr.phase == "error":
        await append_event(session, task, "failed", data)


class EventHub:
    def __init__(self, engine):
        self.engine = engine
        self.connection = None
        self.driver = None
        self.lock = asyncio.Lock()
        self.queues = set()

    def wake(self, *_args):
        for queue in self.queues:
            if queue.empty():
                queue.put_nowait(None)

    async def start(self):
        async with self.lock:
            if self.driver is not None and not self.driver.is_closed():
                return
            if self.connection is not None:
                await self.connection.close()
            self.connection = await self.engine.connect()
            raw = await self.connection.get_raw_connection()
            self.driver = raw.driver_connection
            await self.driver.add_listener("analysis_events", self.wake)

    async def close(self):
        if self.driver is not None and not self.driver.is_closed():
            await self.driver.remove_listener("analysis_events", self.wake)
        if self.connection is not None:
            await self.connection.close()
            self.connection = self.driver = None


def hub_for(engine):
    return _hubs.setdefault(engine, EventHub(engine))


async def close_hubs():
    for hub in list(_hubs.values()):
        await hub.close()
    _hubs.clear()


async def validate_reader(session, principal, task_id=None):
    """Recheck revocation on reconnect and while streaming, never accept tokens in URLs."""
    if task_id is None and principal.role not in ADMIN_ROLES:
        raise HTTPException(403, "无权订阅全部任务")
    user = await session.get(User, principal.user_id, populate_existing=True)
    if user is None or not user.is_active or user.role != principal.role.value:
        raise HTTPException(401, "登录已失效")
    if user.org_id != principal.org_id:
        raise HTTPException(401, "账号企业已变化")
    if user.org_id is not None:
        org = await session.get(Organization, user.org_id, populate_existing=True)
        if org is None or not org.is_active:
            raise HTTPException(401, "企业已停用")
    if task_id is not None:
        task = await session.get(AnalysisTask, task_id, populate_existing=True)
        if task is None or task.org_id != principal.org_id:
            raise HTTPException(404, "合规分析任务不存在")
        return task
    return None


def sse(event_type, data, event_id=None):
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return prefix + f"event: {event_type}\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"


async def stream_events(request, engine, principal, after=0, task_id=None):
    hub = hub_for(engine)
    await hub.start()
    queue = asyncio.Queue(maxsize=1)
    hub.queues.add(queue)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    cursor = after
    try:
        # Subscribe first, then read persisted events: commits during connection setup are replayed.
        yield sse("connected", {"task_id": str(task_id) if task_id else None, "cursor": str(after)})
        while not await request.is_disconnected():
            try:
                async with factory() as session:
                    task = await validate_reader(session, principal, task_id)
                    stmt = select(AnalysisEvent).where(AnalysisEvent.id > cursor)
                    if task_id:
                        stmt = stmt.where(
                            AnalysisEvent.task_id == task_id,
                            AnalysisEvent.org_id == principal.org_id,
                        )
                    events = list(
                        (await session.scalars(stmt.order_by(AnalysisEvent.id).limit(200))).all()
                    )
                    state = task.status if task else None
                # Session released before network writes/backpressure.
                for event in events:
                    cursor = event.id
                    yield sse(
                        event.event_type, {"task_id": str(event.task_id), **event.payload}, cursor
                    )
                if len(events) == 200:
                    continue
                if state in TERMINAL:
                    yield sse("snapshot", {"task_id": str(task_id), "status": state})
                    return
                await hub.start()  # Reconnect LISTEN if the database connection was replaced.
                try:
                    await asyncio.wait_for(queue.get(), timeout=10)
                except TimeoutError:
                    yield ": heartbeat\n\n"
            except HTTPException:
                yield sse("access_revoked", {})
                return
    finally:
        hub.queues.discard(queue)
