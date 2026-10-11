"""Hybrid + graph search over rulings.db and data/embeddings.npy.

  python3 server/search.py "ยกที่ดินให้หลานต้องเสียภาษีไหม" [--tax ภาษีเงินได้บุคคลธรรมดา] [--from 2015]

search()  : full-text (FTS5 trigram, bm25) + semantic (cosine over chunk vectors),
            merged per ruling with reciprocal-rank fusion.
related() : graph neighbours — rulings sharing ข้อกฎหมาย sections/instruments
            (rarer links weigh more, IDF) plus explicit citations either way.
"""
import json
import math
import os
import re
import sqlite3
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "collector"))
from embed import load_env  # noqa: E402

DB = ROOT / "rulings.db"
DATA = ROOT / "data"
RRF_K = 60


@lru_cache(maxsize=1)
def store():
    load_env()
    meta = json.loads((DATA / "embeddings.json").read_text())
    ids = [json.loads(l)["ruling_id"] for l in (DATA / "chunks.jsonl").open(encoding="utf-8")]
    vecs = np.load(DATA / "embeddings.npy").astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con, meta, np.array(ids), vecs


def embed_query(text):
    _, meta, _, _ = store()
    r = requests.post("https://api.openai.com/v1/embeddings", timeout=30,
                      headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                      json={"model": meta["model"], "input": [text], "dimensions": meta["dim"]})
    r.raise_for_status()
    v = np.asarray(r.json()["data"][0]["embedding"], dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def fts_query(text):
    # trigram FTS needs terms of >= 3 chars; quote each so Thai/punctuation is literal
    terms = [t for t in re.split(r"\s+", text.replace('"', " ")) if len(t) >= 3]
    return " OR ".join(f'"{t}"' for t in terms)


def allowed_ids(con, tax=None, year_from=None, year_to=None, section=None):
    where, args = [], []
    if tax:
        where.append("r.tax_type LIKE ?"); args.append(f"%{tax}%")
    if year_from:
        where.append("substr(r.date,1,4) >= ?"); args.append(str(year_from))
    if year_to:
        where.append("substr(r.date,1,4) <= ?"); args.append(str(year_to))
    if section:
        where.append("r.id IN (SELECT ruling_id FROM ruling_section WHERE section = ? OR ref = ?)")
        args += [section, section]
    if not where:
        return None
    return {row[0] for row in con.execute(f"SELECT r.id FROM rulings r WHERE {' AND '.join(where)}", args)}


def search(query, limit=10, tax=None, year_from=None, year_to=None, section=None):
    con, _, chunk_ids, vecs = store()
    allowed = allowed_ids(con, tax, year_from, year_to, section)
    ok = (lambda i: True) if allowed is None else (lambda i: i in allowed)

    fts_rank = {}
    q = fts_query(query)
    if q:
        rows = con.execute("SELECT id FROM rulings_fts WHERE rulings_fts MATCH ? "
                           "ORDER BY bm25(rulings_fts, 3, 2, 1, 1) LIMIT 200", (q,))
        for row in rows:
            if ok(row[0]) and row[0] not in fts_rank:
                fts_rank[row[0]] = len(fts_rank)

    sem_rank, best_part = {}, {}
    sims = vecs @ embed_query(query)
    for i in np.argsort(-sims)[:600]:
        rid = str(chunk_ids[i])
        if ok(rid) and rid not in sem_rank:
            sem_rank[rid] = len(sem_rank)
            best_part[rid] = float(sims[i])

    # Exact text is strong evidence when the query is a term or citation ("สิทธิเก็บกิน",
    # "มาตรา 65 ตรี (13)") or matches only a few rulings — the embedding of a ruling
    # about renting land need not sit near "สิทธิเก็บกิน". Thai has no word spaces, so
    # for a long sentence-style question the trigram side mostly matches noise and
    # only text hits that are also near in meaning count.
    keyword_like = len(query) <= 30 and len(query.split()) <= 3
    citation_like = bool(re.search(r"\d|มาตรา|กค|ป\.", query)) and len(query) < 40
    precise = keyword_like or citation_like
    fts_weight = 1.5 if precise else 0.35
    score = {}
    for rid, r in sem_rank.items():
        score[rid] = 1 / (RRF_K + r)
    for rid, r in fts_rank.items():
        if not precise and sem_rank.get(rid, 10**9) > 150:
            continue  # text-only hit that is not even close in meaning
        score[rid] = score.get(rid, 0) + fts_weight / (RRF_K + r)
    top = sorted(score, key=score.get, reverse=True)[:limit]
    return [dict(summary(con, rid), score=round(score[rid], 4),
                 matched_by=[n for n, d in (("text", fts_rank), ("meaning", sem_rank)) if rid in d],
                 similarity=round(best_part.get(rid, 0), 3)) for rid in top]


def summary(con, rid):
    r = con.execute("SELECT id, doc_no, date, tax_type, subject, law, url FROM rulings WHERE id=?",
                    (rid,)).fetchone()
    return dict(r) if r else {"id": rid}


def get(con, key):
    """By page id or เลขที่หนังสือ (several rulings may share a number)."""
    k = re.sub(r"\s+", "", key)
    k = re.sub(r"^กค\.?", "", k)
    rows = con.execute("SELECT * FROM rulings WHERE id=? OR doc_key=? ORDER BY date", (key, k)).fetchall()
    return [dict(r) for r in rows]


def related(rid, limit=10):
    con, *_ = store()
    n = con.execute("SELECT count(*) FROM rulings").fetchone()[0]
    score = {}
    # shared ข้อกฎหมาย links, weighted by rarity (a link to ม.65 says less than to ม.65 ตรี (13))
    for table, col in (("ruling_section", "ref"), ("ruling_instrument", "ref")):
        for (ref,) in con.execute(f"SELECT DISTINCT {col} FROM {table} WHERE ruling_id=? AND source='law'", (rid,)):
            others = [o for (o,) in con.execute(
                f"SELECT DISTINCT ruling_id FROM {table} WHERE {col}=? AND source='law' AND ruling_id<>?", (ref, rid))]
            w = math.log(n / (1 + len(others)))
            for o in others:
                score[o] = score.get(o, 0) + w
    cites = {o for (o,) in con.execute("SELECT cited_id FROM ruling_cites WHERE ruling_id=? AND cited_id IS NOT NULL", (rid,))}
    cited_by = {o for (o,) in con.execute("SELECT ruling_id FROM ruling_cites WHERE cited_id=?", (rid,))}
    for o in cites | cited_by:
        score[o] = score.get(o, 0) + 100  # explicit citation outranks shared sections
    top = sorted(score, key=score.get, reverse=True)[:limit]
    out = []
    for o in top:
        why = "cites" if o in cites else "cited by" if o in cited_by else "shares ข้อกฎหมาย"
        out.append(dict(summary(con, o), relation=why, weight=round(score[o], 2)))
    return out


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("query")
    p.add_argument("--tax"); p.add_argument("--section")
    p.add_argument("--from", dest="year_from"); p.add_argument("--to", dest="year_to")
    p.add_argument("-n", type=int, default=8)
    a = p.parse_args()
    for h in search(a.query, a.n, a.tax, a.year_from, a.year_to, a.section):
        print(f"{h['doc_no']:<24} {h['date']}  {'+'.join(h['matched_by']):<12} {h['subject'][:70]}")
