"""Embed ruling chunks with OpenAI; only new/changed chunks hit the API.

  python3 collector/embed.py --dry-run   # count chunks and estimate cost
  python3 collector/embed.py

Chunks: per ruling one "header" chunk (เรื่อง + ข้อกฎหมาย) plus the ข้อหารือ and
แนววินิจฉัย split at paragraph boundaries into windows of ~MAX_CHARS. Every chunk
is prefixed with the subject so it carries its context.

Output (committed to git; the server loads it):
  data/chunks.jsonl      one line per chunk: hash, ruling_id, field, part, text
  data/embeddings.npy    float16 [n_chunks, dim], row i = chunks.jsonl line i
  data/embeddings.json   model + dim the vectors were made with
A chunk's hash covers model + dim + text, so the existing file acts as a cache.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
MAX_CHARS = 1500
BATCH = 64


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z_]+)\s*=\s*(.*)\s*$", line)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip().strip("'\"")


def windows(text):
    out, cur = [], ""
    for para in (text or "").split("\n"):
        while len(para) > MAX_CHARS:  # one huge paragraph: hard split
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:MAX_CHARS])
            para = para[MAX_CHARS:]
        if cur and len(cur) + len(para) + 1 > MAX_CHARS:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{para}" if cur else para
    if cur.strip():
        out.append(cur)
    return out


def make_chunks(rows, model, dim):
    chunks = []
    for r in rows:
        head = f"{r.get('doc_no', '')} เรื่อง: {r.get('subject', '')}"
        parts = [("header", f"{head}\nข้อกฎหมาย: {r.get('law') or '-'}")]
        for field, label in (("question", "ข้อหารือ"), ("ruling", "แนววินิจฉัย")):
            for w in windows(r.get(field)):
                parts.append((field, f"{head}\n{label}: {w}"))
        for i, (field, text) in enumerate(parts):
            h = hashlib.sha1(f"{model}|{dim}|{text}".encode()).hexdigest()
            chunks.append({"hash": h, "ruling_id": r["id"], "field": field, "part": i, "text": text})
    return chunks


def embed_batch(texts, model, dim, key):
    for attempt in range(6):
        r = requests.post("https://api.openai.com/v1/embeddings", timeout=120,
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": model, "input": texts, "dimensions": dim})
        if r.status_code == 200:
            return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 ** attempt)
            continue
        sys.exit(f"OpenAI error {r.status_code}: {r.text[:300]}")
    sys.exit("OpenAI: too many retries")


def main(dry_run):
    load_env()
    model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-large")
    dim = int(os.environ.get("OPENAI_EMBED_DIM", "1024"))
    rows = [json.loads(l) for l in (DATA / "rulings.jsonl").open(encoding="utf-8")]
    chunks = make_chunks(rows, model, dim)

    cache = {}
    cf, ef = DATA / "chunks.jsonl", DATA / "embeddings.npy"
    if cf.exists() and ef.exists():
        old = [json.loads(l)["hash"] for l in cf.open(encoding="utf-8")]
        vecs = np.load(ef)
        cache = {h: vecs[i] for i, h in enumerate(old) if i < len(vecs)}
    todo = [c for c in chunks if c["hash"] not in cache]
    chars = sum(len(c["text"]) for c in todo)
    # Thai runs roughly 1 token per 1.5-2.5 chars with OpenAI's tokenizer.
    print(f"chunks {len(chunks)}, cached {len(chunks) - len(todo)}, to embed {len(todo)} "
          f"(~{chars / 2e6:.1f}-{chars / 1.2e6:.1f}M tokens)")
    if dry_run or not todo:
        if not dry_run:
            print("nothing to embed")
        return
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY missing (.env)")

    done = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        for c, v in zip(batch, embed_batch([c["text"] for c in batch], model, dim, key)):
            cache[c["hash"]] = np.asarray(v, dtype=np.float16)
        done += len(batch)
        if done % (BATCH * 20) == 0 or done == len(todo):
            print(f"  {done}/{len(todo)}", flush=True)
            save(chunks, cache, model, dim, partial=True)
    save(chunks, cache, model, dim)


def save(chunks, cache, model, dim, partial=False):
    keep = [c for c in chunks if c["hash"] in cache]  # partial saves keep progress
    with (DATA / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in keep:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    np.save(DATA / "embeddings.npy",
            np.stack([cache[c["hash"]] for c in keep]) if keep else np.zeros((0, dim), np.float16))
    (DATA / "embeddings.json").write_text(json.dumps({"model": model, "dim": dim}))
    if not partial:
        print(f"saved {len(keep)} chunk vectors")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    main(p.parse_args().dry_run)
