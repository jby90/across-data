"""Model transport is mocked; originals and evidence remain in the isolated database."""

import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from app.analysis import llm
from app.core.egress_models import LlmCallAudit
from app.core.redaction import Redactor, llm_scope, redact

from .conftest import bearer, login


@pytest.mark.parametrize(
    "value",
    [
        "person@example.com",
        "13800138000",
        "+86 138-0013-8000",
        "+65 6123 4567",
        "110101199003071234",
        "S1234567A",
        "LHGCM82633A004352",
        "京AD12345",
        "192.168.11.10",
        "2001:db8::23",
        "6222 0212 3456 7890",
        "6123 4567",
    ],
)
def test_identifiers_are_reversible(value):
    source = f"用户提供：{value}。"
    safe, mapping = redact(source)
    assert value not in safe
    r = Redactor()
    r.mapping = mapping
    assert r.restore(safe) == source


def test_aliases_overlap_json_escaping_and_placeholders_are_call_local():
    secret = 'Example "Car" Ltd'
    source = f"{secret}，Project-ALPHA，a@example.com；{secret}"
    first, second = Redactor([secret, "Project-ALPHA"]), Redactor([secret])
    safe = first.tree({"fact_key": source})
    assert first.restore(safe) == {"fact_key": source}
    assert secret not in safe["fact_key"] and "Project-ALPHA" not in safe["fact_key"]
    assert second.restore(safe) == safe  # No cross-request mapping or guessed restoration.
    assert first.counts["ORG"] == 3


def test_email_sentence_punctuation_and_malformed_placeholder():
    r = Redactor()
    source = "Contact person@example.com."
    safe = r.redact(source)
    assert "person@example.com" not in safe and r.restore(safe) == source
    with pytest.raises(ValueError, match="占位符损坏"):
        r.restore(r.prefix + "UNKNOWN__")


@pytest.mark.parametrize("mode", ["on", "off"])
async def test_real_transport_audits_metadata_and_restores_before_validation(
    monkeypatch,
    session_factory,
    seed,
    mode,
):
    settings = llm.get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "synthetic-test-only")
    monkeypatch.setattr(settings, "llm_redaction", mode)
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:29999/v1")
    source = 'Alpha "Project" 13800138000 person@example.com LHGCM82633A004352 192.168.1.6'
    observed = []
    client_type = httpx.AsyncClient

    def handler(request):
        assert str(request.url) == "http://127.0.0.1:29999/v1/chat/completions"
        body = json.loads(request.content)
        fact = json.loads(body["messages"][0]["content"])["fact"]
        observed.append(fact)
        if mode == "on":
            assert all(
                s not in fact
                for s in [
                    "Alpha",
                    "13800138000",
                    "person@example.com",
                    "LHGCM82633A004352",
                    "192.168.1.6",
                ]
            )
        else:
            assert fact == source
        return httpx.Response(
            200,
            json={
                "model": "synthetic",
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                },
                "choices": [{"message": {"content": json.dumps({"quote": fact})}}],
            },
        )

    monkeypatch.setattr(
        llm.httpx,
        "AsyncClient",
        lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw),
    )
    with llm_scope(
        org_id=seed["org_a"].id, aliases=['Alpha "Project"'], session_factory=session_factory
    ):
        result = await llm.call_llm_json(
            [{"role": "user", "content": json.dumps({"fact": source})}]
        )
    assert result.data["quote"] == source  # Exact original, including JSON quotation marks.
    async with session_factory() as s:
        records = list((await s.scalars(select(LlmCallAudit))).all())
        assert len(records) == 2 and {r.stage for r in records} == {"started", "success"}
        assert all(r.org_id == seed["org_a"].id for r in records)
        assert sum(r.tokens_in + r.tokens_out for r in records) == 18
        assert all(len(r.input_hash) == 64 for r in records)
        for row in records:
            meta = json.dumps({k: str(v) for k, v in vars(row).items() if not k.startswith("_")})
            assert (
                source not in meta
                and "13800138000" not in meta
                and "person@example.com" not in meta
            )
            assert "synthetic-test-only" not in meta and "__CYSH_SECRET_" not in meta
        assert bool(records[0].redaction_hits) is (mode == "on")


async def test_failed_start_audit_prevents_network_call(monkeypatch):
    class BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("synthetic audit outage")

        async def __aexit__(self, *_):
            pass

    def forbidden(**_):
        pytest.fail("Audit failure must prevent transmission")

    monkeypatch.setattr(llm.httpx, "AsyncClient", forbidden)
    with llm_scope(session_factory=BrokenSession):
        with pytest.raises(llm.LlmUnavailableError, match="未发送请求"):
            await llm.post_model({"model": "test", "messages": []}, timeout=1, purpose="test")


async def test_concurrent_tenant_mappings_do_not_mix(monkeypatch, session_factory, seed):
    settings = llm.get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "synthetic-only")
    client_type = httpx.AsyncClient

    async def handler(request):
        await asyncio.sleep(0.01)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"echo": body["messages"][0]["content"]})}}
                ]
            },
        )

    monkeypatch.setattr(
        llm.httpx,
        "AsyncClient",
        lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw),
    )

    async def invoke(org, value):
        with llm_scope(org_id=org.id, aliases=[value], session_factory=session_factory):
            return (await llm.call_llm_json([{"role": "user", "content": value}])).data

    a, b = await asyncio.gather(
        invoke(seed["org_a"], "Tenant Alpha"), invoke(seed["org_b"], "Tenant Beta")
    )
    assert a == {"echo": "Tenant Alpha"} and b == {"echo": "Tenant Beta"}


async def test_tenant_terms_endpoints_authorize_and_keep_change_history(
    client, seed, session_factory
):
    admin = bearer(await login(client, "admin", "admin@example.com"))
    reviewer = bearer(await login(client, "admin", "reviewer@example.com"))
    member = bearer(await login(client, "user", "a-member@example.com"))
    org_admin = bearer(await login(client, "user", "a-admin@example.com"))
    url = f"/api/admin/orgs/{seed['org_b'].id}/redaction"
    assert (
        await client.patch(url, headers=reviewer, json={"terms": ["SECRET"]})
    ).status_code == 403
    assert (await client.patch(url, headers=member, json={"terms": ["SECRET"]})).status_code == 401
    r = await client.patch(
        "/api/user/organization/redaction", headers=org_admin, json={"terms": ["Project-Alpha"]}
    )
    assert r.status_code == 200 and r.json()["id"] == str(seed["org_a"].id)
    r = await client.patch(url, headers=admin, json={"terms": ["Project-Beta"]})
    assert r.status_code == 200 and r.json()["llm_redaction_terms"] == ["Project-Beta"]
    assert (await client.patch(url, headers=admin, json={"terms": ["x"]})).status_code == 422
    from app.knowledge.models import KnowledgeAuditRecord

    async with session_factory() as s:
        changes = list(
            (
                await s.scalars(
                    select(KnowledgeAuditRecord).where(
                        KnowledgeAuditRecord.action == "redaction_terms_changed"
                    )
                )
            ).all()
        )
        assert len(changes) == 2
