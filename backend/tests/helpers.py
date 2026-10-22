"""测试辅助:手工构造最小 PDF,避免引入 PDF 生成库。"""

import httpx

from app.ingestion.fetcher import PoliteFetcher


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]]) -> bytes:
    """每页一组 ASCII 文本行。传入空列表的页没有文字层(模拟扫描件)。"""
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, lines in enumerate(pages):
        content_id = 5 + 2 * i
        objs.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                "/Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode()
        )
        if lines:
            body = (
                "BT /F1 11 Tf 14 TL 50 740 Td "
                + " ".join(f"({_esc(line)}) Tj T*" for line in lines)
                + " ET"
            )
        else:
            body = ""
        data = body.encode("latin-1")
        objs.append(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for idx, body_bytes in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % idx + body_bytes + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1) + b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref,
    )
    return bytes(out)


async def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]  # 文档示例用的公网地址


def make_fetcher(handler, *, delay: float = 0.0, **kwargs) -> PoliteFetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PoliteFetcher(
        client,
        user_agent="CheyanShuheBot/0.1 (test)",
        delay_seconds=delay,
        resolver=public_resolver,
        **kwargs,
    )
