"""Build rulings.db from data/rulings.jsonl.

  python3 build_db.py            # rebuild tables + relations, print report
  python3 build_db.py --report   # also write data/unextracted_law.jsonl

Everything here is derived and cheap, so it is rebuilt from scratch each run
(the Docker build runs it too). Embeddings live in data/ (embed.py), not here.

Tables
  rulings          one row per ruling (from rulings.jsonl)
  rulings_fts      FTS5 trigram index over subject/law/question/ruling
                   (trigram = substring search, works for Thai; queries >= 3 chars)
  ruling_section   ruling -> ประมวลรัษฎากร section ("65 ตรี", "81") with the
                   full reference ("65 ตรี (13)") and where it was found
  ruling_instrument ruling -> คำสั่ง / ประกาศ / พ.ร.ฎ. / กฎกระทรวง
  ruling_cites     ruling -> another ruling's เลขที่หนังสือ (resolved when unique)
"""
import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB = ROOT / "rulings.db"
SRC = ROOT / "data" / "rulings.jsonl"

SCHEMA = """
DROP TABLE IF EXISTS rulings;
DROP TABLE IF EXISTS rulings_fts;
DROP TABLE IF EXISTS ruling_section;
DROP TABLE IF EXISTS ruling_instrument;
DROP TABLE IF EXISTS ruling_cites;
CREATE TABLE rulings (
  id TEXT PRIMARY KEY, doc_no TEXT, doc_key TEXT, date TEXT, date_th TEXT,
  listed_year INTEGER, listed_month TEXT, tax_type TEXT, subject TEXT, law TEXT,
  question TEXT, ruling TEXT, cabinet_no TEXT, incomplete TEXT, url TEXT);
CREATE INDEX rulings_doc_key ON rulings(doc_key);
CREATE INDEX rulings_tax ON rulings(tax_type);
CREATE INDEX rulings_date ON rulings(date);
CREATE VIRTUAL TABLE rulings_fts USING fts5(
  id UNINDEXED, subject, law, question, ruling, tokenize='trigram');
CREATE TABLE ruling_section (
  ruling_id TEXT, section TEXT, ref TEXT, source TEXT);
CREATE INDEX rs_section ON ruling_section(section);
CREATE INDEX rs_ruling ON ruling_section(ruling_id);
CREATE TABLE ruling_instrument (
  ruling_id TEXT, kind TEXT, ref TEXT, source TEXT);
CREATE INDEX ri_ref ON ruling_instrument(ref);
CREATE TABLE ruling_cites (
  ruling_id TEXT, cited_doc_no TEXT, cited_id TEXT);
CREATE INDEX rc_cited ON ruling_cites(cited_id);
"""

THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
SUFFIX = r"(?:ทวิ|ตรี|จัตวา|เบญจ|ฉ|สัตต|อัฏฐ|นว|ทศ)"
SECTION_RE = re.compile(
    rf"(?:มาตรา|ม\.)\s*(\d+(?:/\d+)?(?:\s*{SUFFIX})?)((?:\s*\([^()\s]{{1,4}}\))*)")
# A bare "(1)(ง)" after "และ"/"," continues the previous section: "มาตรา 81(2)(ก) และ (1)(ง)"
CONT_RE = re.compile(r"^\s*(?:,|และ|หรือ)\s*((?:\([^()\s]{1,4}\))+)")

INSTRUMENT_RES = [
    ("คำสั่ง", re.compile(r"(?:คำสั่ง(?:กรมสรรพากร)?\s*(?:ที่\s*)?)?((?:ท\.)?ป\.\s*\d+\s*/\s*\d{4})")),
    ("พ.ร.ฎ.", re.compile(r"(?:พระราชกฤษฎีกา|พ\.ร\.ฎ\.)[^()]{0,80}?\(?\s*ฉบับที่\s*(\d+)")),
    ("ลักษณะตราสาร", re.compile(r"(?:ลักษณะ(?:แห่ง)?)?ตราสาร(?:ที่)?\s*(\d+)\s*\.?\s*((?:\([ก-ฮ\d]{1,3}\))*)")),
    ("กฎกระทรวง", re.compile(r"กฎกระทรวง[^()]{0,40}?ฉบับที่\s*(\d+)")),
    ("ประกาศอธิบดี", re.compile(
        r"ประกาศอธิบดีกรมสรรพากร\s*ฯ?\s*(?:เกี่ยวกับ\s*(ภาษี\S+?|อากรแสตมป์))?\s*\(?\s*ฉบับที่\s*(\d+)")),
]
DOC_RE = re.compile(r"กค\s*\.?\s*(\d{4}(?:\s*\([^)]{1,12}\))?(?:\s*/\s*[ก-ฮ]\.)?)\s*/\s*(\d+)")


def norm(s):
    return re.sub(r"\s+", " ", (s or "").translate(THAI_DIGITS)).strip()


def doc_key(doc_no):
    """'กค 0702(กม.14)/262' -> '0702(กม.14)/262'; spacing and 'กค' prefix dropped."""
    s = norm(doc_no).replace(" ", "")
    s = re.sub(r"^กค\.?", "", s)
    return s or None


def sections(text):
    text = norm(text)
    out = []
    for m in SECTION_RE.finditer(text):
        base = re.sub(r"\s+", " ", m.group(1)).strip()
        subs = re.sub(r"\s+", "", m.group(2))
        out.append((base, f"{base} {subs}".strip()))
        tail = text[m.end():]
        while (c := CONT_RE.match(tail)):
            out.append((base, f"{base} {re.sub(r'\s+', '', c.group(1))}"))
            tail = tail[c.end():]
    return out


def instruments(text):
    text = norm(text)
    out = []
    for kind, rx in INSTRUMENT_RES:
        for m in rx.finditer(text):
            if kind == "คำสั่ง":
                ref = "คำสั่ง " + re.sub(r"\s+", "", m.group(1))
            elif kind == "ประกาศอธิบดี":
                about = f" เกี่ยวกับ{m.group(1)}" if m.group(1) else ""
                ref = f"ประกาศอธิบดีฯ{about} ฉบับที่ {m.group(2)}"
            elif kind == "ลักษณะตราสาร":
                ref = f"ลักษณะตราสาร {m.group(1)}{m.group(2)}"  # บัญชีอัตราอากรแสตมป์
            else:
                ref = f"{kind} ฉบับที่ {m.group(1)}"
            out.append((kind, ref))
    return out


def build(report):
    rows = [json.loads(l) for l in SRC.open(encoding="utf-8")]
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)

    by_key = {}
    for r in rows:
        k = doc_key(r.get("doc_no"))
        by_key.setdefault(k, []).append(r["id"])

    unextracted = []
    for r in rows:
        k = doc_key(r.get("doc_no"))
        con.execute("INSERT INTO rulings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            r["id"], r.get("doc_no"), k, r.get("date"), r.get("date_th"),
            int(r["listed_year"]) if r.get("listed_year") else None, r.get("listed_month"),
            r.get("tax_type"), r.get("subject"), r.get("law"), r.get("question"),
            r.get("ruling"), r.get("cabinet_no"),
            ",".join(r["incomplete"]) if r.get("incomplete") else None, r.get("url")))
        con.execute("INSERT INTO rulings_fts VALUES (?,?,?,?,?)", (
            r["id"], r.get("subject"), r.get("law"), r.get("question"), r.get("ruling")))

        found_law = False
        for source in ("law", "question", "ruling"):
            secs = set(sections(r.get(source)))
            insts = set(instruments(r.get(source)))
            if source == "law":
                found_law = bool(secs or insts)
            con.executemany("INSERT INTO ruling_section VALUES (?,?,?,?)",
                            [(r["id"], b, ref, source) for b, ref in sorted(secs)])
            con.executemany("INSERT INTO ruling_instrument VALUES (?,?,?,?)",
                            [(r["id"], kd, ref, source) for kd, ref in sorted(insts)])
        # Some 2540-41 pages put the word "ประเด็นปัญหา" (or "-") in the law cell itself.
        if r.get("law") and r["law"].strip(" -") not in ("", "ประเด็นปัญหา", "มาตรา") and not found_law:
            unextracted.append({"id": r["id"], "law": r["law"]})

        cited = set()
        for m in DOC_RE.finditer(norm((r.get("question") or "") + " " + (r.get("ruling") or ""))):
            ck = re.sub(r"\s+", "", f"{m.group(1)}/{m.group(2)}")
            if ck == k or ck in cited:
                continue
            cited.add(ck)
            hits = by_key.get(ck, [])
            con.execute("INSERT INTO ruling_cites VALUES (?,?,?)",
                        (r["id"], ck, hits[0] if len(hits) == 1 else None))
    con.commit()

    q = lambda sql: con.execute(sql).fetchone()[0]
    n = len(rows)
    with_law = sum(1 for r in rows if (r.get("law") or "").strip(" -") not in ("", "ประเด็นปัญหา", "มาตรา"))
    print(f"rulings {n}  (with ข้อกฎหมาย {with_law})")
    print(f"ข้อกฎหมาย extracted: {with_law - len(unextracted)}/{with_law} "
          f"({100 * (with_law - len(unextracted)) / max(with_law, 1):.1f}%)")
    print(f"section links {q('SELECT count(*) FROM ruling_section')} "
          f"({q('SELECT count(DISTINCT section) FROM ruling_section')} distinct sections)")
    print(f"instrument links {q('SELECT count(*) FROM ruling_instrument')} "
          f"({q('SELECT count(DISTINCT ref) FROM ruling_instrument')} distinct)")
    print(f"citations {q('SELECT count(*) FROM ruling_cites')}, "
          f"resolved to a unique ruling {q('SELECT count(*) FROM ruling_cites WHERE cited_id IS NOT NULL')}")
    dup = sum(1 for v in by_key.values() if len(v) > 1)
    print(f"เลขที่หนังสือ shared by >1 ruling: {dup}")
    if report:
        with (ROOT / "data" / "unextracted_law.jsonl").open("w", encoding="utf-8") as f:
            for u in unextracted:
                f.write(json.dumps(u, ensure_ascii=False) + "\n")
        print("wrote data/unextracted_law.jsonl")
    con.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--report", action="store_true")
    build(p.parse_args().report)
