"""MCP server: search the Revenue Department's ข้อหารือภาษีอากร.

  python3 server/app.py            # HTTP on $PORT (default 8790), /mcp behind a bearer token

Tokens: RD_TOKENS="korn:tok_xxx,somchai:tok_yyy" (one per person, so one can be
withdrawn alone); MCP_AUTH_TOKEN alone also works, as user "owner".
OPENAI_API_KEY is needed at query time to embed the question.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

sys.path.insert(0, str(Path(__file__).resolve().parent))
import search  # noqa: E402

PORT = int(os.environ.get("PORT") or 8790)
HOST = os.environ.get("HOST") or "0.0.0.0"
SNIPPET = 400

server = MCPServer(
    name="rd-rulings",
    version="0.1.0",
    instructions=(
        "Search tool for ข้อหารือภาษีอากร — the Thai Revenue Department's published tax rulings "
        "(2540–present, ~4,600 rulings), each with เลขที่หนังสือ, date, ข้อหารือ (facts and question) and "
        "แนววินิจฉัย (the Department's answer).\n\n"
        "How to research: run search_tax_rulings several times with different phrasings — the facts in "
        "plain Thai, the legal concept, and the มาตรา if you know it. Read the แนววินิจฉัย of the promising "
        "hits with get_tax_ruling before relying on them; the snippet is not enough. Use related_rulings "
        "on the best hit to find rulings on the same provisions, and rulings_by_law to list everything on "
        "a มาตรา or คำสั่ง/ประกาศ/พ.ร.ฎ.\n\n"
        "When answering, cite each ruling by its เลขที่หนังสือ and date and give its url. A ruling answers "
        "one taxpayer's facts and the law may have changed since — say so, prefer recent rulings, and "
        "point out where rulings disagree. Never state a ruling's content you did not read here."
    ),
)


def be_year(y):
    """Accept พ.ศ. or ค.ศ.; the index stores ค.ศ."""
    if y in (None, ""):
        return None
    y = int(y)
    return y - 543 if y > 2400 else y


def with_snippet(con, hits):
    for h in hits:
        row = con.execute("SELECT ruling FROM rulings WHERE id=?", (h["id"],)).fetchone()
        text = (row[0] or "") if row else ""
        h["ruling_snippet"] = text[:SNIPPET] + ("…" if len(text) > SNIPPET else "")
        h.pop("score", None)
    return hits


def resolve(key):
    con, *_ = search.store()
    rows = search.get(con, str(key).strip())
    if not rows:
        raise ToolError(f"No ruling found for {key!r}. Give a เลขที่หนังสือ such as 'กค 0702/4710' "
                        "or the id returned by search_tax_rulings.")
    return con, rows


@server.tool(
    title="Search Thai tax rulings (ข้อหารือภาษีอากร)",
    description=(
        "Hybrid search (meaning + exact words) over the Revenue Department's tax rulings. `query` can be "
        "a question or facts in Thai or English, a concept, a มาตรา ('มาตรา 65 ตรี (13)') or a เลขที่หนังสือ. "
        "Optional filters: tax_type (e.g. 'ภาษีมูลค่าเพิ่ม', 'ภาษีเงินได้นิติบุคคล', 'อากรแสตมป์'), "
        "year_from / year_to (พ.ศ. or ค.ศ., by ruling date), section (ประมวลรัษฎากร section, e.g. '81' "
        "or '65 ตรี (13)'). A short legal term ('สิทธิเก็บกิน', 'ภาระจำยอม') is matched as exact text "
        "anywhere in the ruling; a sentence is matched by meaning — so search both the key term alone "
        "and the facts as a sentence. Returns up to `limit` rulings with id, เลขที่หนังสือ, date, subject, ข้อกฎหมาย, "
        "url and the start of the แนววินิจฉัย; call get_tax_ruling for the full text."
    ),
)
def search_tax_rulings(query: str, limit: int = 8, tax_type: str | None = None,
                       year_from: int | None = None, year_to: int | None = None,
                       section: str | None = None) -> list[dict]:
    if not query or not query.strip():
        raise ToolError("query is empty")
    limit = max(1, min(int(limit), 25))
    section = normalise_section(section) if section else None
    con, *_ = search.store()
    hits = search.search(query.strip(), limit, tax_type, be_year(year_from), be_year(year_to), section)
    return with_snippet(con, hits)


@server.tool(
    title="Get the full text of a tax ruling",
    description=(
        "Full ruling: เลขที่หนังสือ, date, subject, ข้อกฎหมาย, ข้อหารือ, แนววินิจฉัย, url. `ruling` is a "
        "เลขที่หนังสือ ('กค 0702/4710', '0702/4710') or an id from search results. One number can belong "
        "to rulings of different years; all of them are returned, oldest first."
    ),
)
def get_tax_ruling(ruling: str) -> list[dict]:
    _, rows = resolve(ruling)
    return [{k: v for k, v in r.items() if k not in ("doc_key",) and v not in (None, "")} for r in rows]


@server.tool(
    title="Rulings on a provision or instrument",
    description=(
        "Every ruling whose ข้อกฎหมาย cites `reference`: a ประมวลรัษฎากร section ('81', '65 ตรี', "
        "'65 ตรี (13)', 'มาตรา 40 (8)') or an instrument ('ท.ป.4/2528', 'ป.36/2536', 'พ.ร.ฎ. ฉบับที่ 18', "
        "'กฎกระทรวง ฉบับที่ 126', 'ประกาศอธิบดีฯ เกี่ยวกับภาษีมูลค่าเพิ่ม ฉบับที่ 29', 'ลักษณะตราสาร 28(ข)'). "
        "Newest first."
    ),
)
def rulings_by_law(reference: str, limit: int = 30) -> dict:
    con, *_ = search.store()
    limit = max(1, min(int(limit), 100))
    ref = reference.strip()
    sec = normalise_section(ref) if re.search(r"^(มาตรา|ม\.)?\s*\d", ref) else None
    if sec:
        ids = con.execute("SELECT DISTINCT ruling_id FROM ruling_section WHERE source='law' "
                          "AND (section=? OR ref=?)", (sec, sec)).fetchall()
        matched = f"มาตรา {sec}"
    else:
        key = re.sub(r"\s+", "", ref).replace("คำสั่ง", "").replace("พระราชกฤษฎีกา", "พ.ร.ฎ.")
        ids = con.execute("SELECT DISTINCT ruling_id FROM ruling_instrument WHERE source='law' "
                          "AND replace(ref,' ','') LIKE ?", (f"%{key}%",)).fetchall()
        matched = ref
    ids = [i[0] for i in ids]
    if not ids:
        raise ToolError(f"No ruling cites {reference!r} in its ข้อกฎหมาย. Try a broader form "
                        "('65 ตรี' instead of '65 ตรี (13)') or search_tax_rulings.")
    rows = con.execute(f"SELECT id, doc_no, date, tax_type, subject, url FROM rulings WHERE id IN "
                       f"({','.join('?' * len(ids))}) ORDER BY date DESC LIMIT ?", (*ids, limit)).fetchall()
    return {"reference": matched, "total": len(ids), "rulings": [dict(r) for r in rows]}


@server.tool(
    title="Rulings related to a ruling",
    description=(
        "Graph neighbours of a ruling: rulings it cites or that cite it, then rulings sharing its "
        "ข้อกฎหมาย (rarer provisions weigh more). `ruling` is a เลขที่หนังสือ or id; if the number "
        "belongs to several rulings, pass the id."
    ),
)
def related_rulings(ruling: str, limit: int = 10) -> list[dict]:
    _, rows = resolve(ruling)
    if len(rows) > 1:
        raise ToolError("That เลขที่หนังสือ belongs to several rulings: " + ", ".join(
            f"id {r['id']} ({r['date']}: {r['subject'][:50]})" for r in rows) + ". Pass one id.")
    return search.related(rows[0]["id"], max(1, min(int(limit), 30)))


def normalise_section(s):
    """'มาตรา 65 ตรี(13)' / 'ม.65ตรี (13)' -> '65 ตรี (13)', the form ruling_section stores."""
    s = re.sub(r"^(มาตรา|ม\.)\s*", "", s.strip())
    s = re.sub(r"\s+", "", s)
    m = re.match(r"(\d+(?:/\d+)?)(ทวิ|ตรี|จัตวา|เบญจ|ฉ|สัตต|อัฏฐ|นว|ทศ)?(.*)", s)
    if not m:
        return s
    base = m.group(1) + (f" {m.group(2)}" if m.group(2) else "")
    return f"{base} {m.group(3)}".strip()


@server.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> Response:
    con, meta, ids, _ = search.store()
    n = con.execute("SELECT count(*) FROM rulings").fetchone()[0]
    latest = con.execute("SELECT max(date) FROM rulings").fetchone()[0]
    return JSONResponse({"ok": True, "rulings": n, "latest": latest, "chunks": len(ids), "model": meta["model"]})


def read_tokens() -> dict[str, str]:
    tokens = {}
    for pair in (os.environ.get("RD_TOKENS") or "").split(","):
        name, sep, tok = pair.strip().partition(":")
        if sep and name.strip() and tok.strip():
            tokens[tok.strip()] = name.strip()
    single = (os.environ.get("MCP_AUTH_TOKEN") or "").strip()
    if single:
        tokens.setdefault(single, "owner")
    return tokens


class BearerAuth:
    """Plain ASGI, as in juraXjk-legal-doc-mcp: BaseHTTPMiddleware does not sit well
    in front of the MCP transport's streamed responses."""

    def __init__(self, app, tokens):
        self.app, self.tokens = app, tokens

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/health":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        token = headers.get("authorization", "").removeprefix("Bearer ").removeprefix("bearer ").strip()
        user = self.tokens.get(token)
        if not user:
            return await JSONResponse({"error": "a valid bearer token is required"}, status_code=401,
                                      headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        print(f"{user} {scope.get('method')} {scope.get('path')}", file=sys.stderr)
        await self.app(scope, receive, send)


def create_app(tokens=None):
    # The bearer token guards this server; Render's proxy would fail the default
    # DNS-rebinding host check, so it is off (same reasoning as legal-doc-mcp).
    app = server.streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return BearerAuth(app, read_tokens() if tokens is None else tokens)


def main():
    import uvicorn
    search.load_env()
    tokens = read_tokens()
    if not tokens:
        sys.exit('Refusing to start: set RD_TOKENS="name:token" (or MCP_AUTH_TOKEN) first.')
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Refusing to start: OPENAI_API_KEY is needed to embed queries.")
    search.store()  # load the index now, not on the first request
    print(f"rd-rulings on http://{HOST}:{PORT}/mcp — {len(tokens)} tokens", file=sys.stderr)
    uvicorn.run(create_app(tokens), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
