"""Retrieval check: python3 eval/run.py  — hit@5 and MRR@10 over eval/queries.jsonl.
Add real questions there (expect = เลขที่หนังสือ without 'กค') and re-run after any search change."""
import json, re, sys
from functools import lru_cache
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import search

search.embed_query = lru_cache(maxsize=None)(search.embed_query)  # one API call per query


def key(doc_no):
    return re.sub(r"^กค\.?", "", re.sub(r"\s+", "", doc_no or ""))


def run(label=""):
    qs = [json.loads(l) for l in (ROOT / "eval" / "queries.jsonl").open(encoding="utf-8")]
    hits = mrr = 0
    misses = []
    for item in qs:
        got = [key(h["doc_no"]) for h in search.search(item["q"], 10)]
        want = {key(e) for e in item["expect"]}
        ranks = [i for i, g in enumerate(got) if g in want]
        hits += bool(ranks and ranks[0] < 5)
        mrr += 1 / (ranks[0] + 1) if ranks else 0
        if not ranks or ranks[0] >= 5:
            misses.append(f"{item['q'][:40]} -> {'#' + str(ranks[0] + 1) if ranks else 'not in top 10'}")
    print(f"{label:<28} hit@5 {hits}/{len(qs)}  MRR {mrr / len(qs):.2f}")
    for m in misses:
        print("    miss:", m)


if __name__ == "__main__":
    run()
