"""Reversible in-memory redaction. Mappings never belong in logs or audit rows."""

import ipaddress
import re
import secrets
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class EgressContext:
    org_id: object = None
    aliases: tuple[str, ...] = ()
    session_factory: object = None


_EMPTY_CONTEXT = EgressContext()
egress_context = ContextVar("llm_egress", default=_EMPTY_CONTEXT)


@contextmanager
def llm_scope(*, org_id=None, aliases=(), session_factory=None):
    token = egress_context.set(EgressContext(org_id, tuple(aliases), session_factory))
    try:
        yield
    finally:
        egress_context.reset(token)


def bind_egress(session, org=None):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    egress_context.set(
        EgressContext(
            org.id if org else None,
            tuple([org.name, *(org.llm_redaction_terms or [])]) if org else (),
            async_sessionmaker(session.bind, expire_on_commit=False),
        )
    )


class Redactor:
    def __init__(self, aliases=(), *, prefix=None):
        self.prefix = prefix or ("__CYSH_SECRET_" + secrets.token_hex(8).upper() + "_")
        self.mapping = {}
        self.counts = Counter()
        self.aliases = sorted(set(a.strip() for a in aliases if a.strip()), key=len, reverse=True)
        self.patterns = [
            ("EMAIL", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w-])")),
            ("ID", re.compile(r"(?<!\d)\d{17}[\dXx](?!\w)|(?<!\w)[STFGM]\d{7}[A-Z](?!\w)")),
            (
                "VIN",
                re.compile(
                    r"(?<![A-Za-z0-9])(?=[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9]))"
                    r"(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])(?=[A-HJ-NPR-Z0-9]*\d)"
                    r"[A-HJ-NPR-Z0-9]{17}"
                ),
            ),
            (
                "PLATE",
                re.compile(
                    r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼]"
                    r"[A-Z][A-Z0-9]{5,6}(?![A-Z0-9])"
                ),
            ),
            ("BANK", re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)")),
            (
                "PHONE",
                re.compile(
                    r"(?<!\w)(?:\+?86[ -]?)?1[3-9](?:[ -]?\d){9}(?!\w)|"
                    r"(?<!\w)\+\d{1,3}[ -]?(?:\d[ -]?){5,13}\d(?!\w)|"
                    r"(?<!\w)\d{4}[ -]\d{4}(?!\w)"
                ),
            ),
            (
                "IP",
                re.compile(
                    r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])|"
                    r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])"
                ),
            ),
        ]

    def token(self, value, kind):
        existing = next((k for k, v in self.mapping.items() if v == value), None)
        self.counts[kind] += 1
        if existing:
            return existing
        # Alphabetic suffixes avoid numeric-protection transforms in translation.
        n, suffix = len(self.mapping), ""
        while True:
            n, r = divmod(n, 26)
            suffix = chr(65 + r) + suffix
            if not n:
                break
        marker = self.prefix + suffix + "__"
        self.mapping[marker] = value
        return marker

    def redact(self, text):
        # A single substitution over the original avoids re-matching placeholders.
        rules = [("ORG", re.compile("|".join(map(re.escape, self.aliases)), re.I))]
        if not self.aliases:
            rules = []
        rules += self.patterns
        spans = []
        for priority, (kind, pattern) in enumerate(rules):
            for m in pattern.finditer(text):
                if kind == "IP":
                    try:
                        ipaddress.ip_address(m.group())
                    except ValueError:
                        continue
                spans.append((m.start(), priority, -len(m.group()), m.end(), kind, m.group()))
        end, parts = 0, []
        for start, _, _, stop, kind, value in sorted(spans):
            if start < end:
                continue
            parts.extend((text[end:start], self.token(value, kind)))
            end = stop
        return "".join(parts) + text[end:]

    def restore(self, value):
        if isinstance(value, str):
            for marker, original in self.mapping.items():
                value = value.replace(marker, original)
            if self.prefix in value:
                raise ValueError("模型返回的隐私占位符损坏，禁止作为原文使用")
            return value
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        if isinstance(value, dict):
            return {k: self.restore(v) for k, v in value.items()}
        return value

    def tree(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.tree(v) for v in value]
        if isinstance(value, dict):
            return {k: self.tree(v) for k, v in value.items()}
        return value


def redact(text, aliases=()):
    r = Redactor(aliases)
    return r.redact(text), r.mapping
