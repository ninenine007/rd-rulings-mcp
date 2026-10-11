# rd-rulings-mcp — ข้อหารือภาษีอากร archive + search

Local archive of the Revenue Department's ข้อหารือ (www.rd.go.th, 2540 onward).

```
python3 collector/rd_rulings.py crawl                  # all years; skips rulings already saved
python3 collector/rd_rulings.py crawl --years 2569     # just one year (monthly top-up)
python3 collector/rd_rulings.py parse                  # rebuild data/ from raw/
```

- `collector/raw/<id>.html` (local only, git-ignored) — page as downloaded (the source of truth; re-parse any time)
- `collector/index.jsonl` — where each ruling was listed (year, month, url)
- `data/rulings.jsonl` — one ruling per line: `doc_no, date_th, date (ISO), subject,
  tax_type, law, question, ruling, cabinet_no (เลขตู้, older years), url, listed_year/month`;
  `incomplete` lists fields that are blank on the source page itself
- `data/parse_errors.jsonl` — pages missing เลขที่หนังสือ or เรื่อง

Older pages hard-wrap text with `<br>`; the parser rejoins lines and only starts a new
paragraph at an `&nbsp;` indent or a list marker (`1.`, `(1)`, `ก.`).

## Database (`rulings.db`)

`python3 collector/build_db.py --report` rebuilds it from `data/rulings.jsonl` in seconds.
`collector/update.sh` does the whole monthly top-up (crawl this + last year → parse → build).

- `rulings` + `rulings_fts` — full-text search (FTS5 trigram; Thai works, query ≥ 3 chars)
- `ruling_section` — ruling → ประมวลรัษฎากร section (`section` "65 ตรี", `ref` "65 ตรี (13)",
  `source` law/question/ruling)
- `ruling_instrument` — ruling → คำสั่ง ป./ท.ป., พ.ร.ฎ., กฎกระทรวง, ประกาศอธิบดีฯ, ลักษณะตราสาร
- `ruling_cites` — ruling → another ruling's เลขที่หนังสือ (`cited_id` only when unique;
  the same number recurs across years)
- `data/unextracted_law.jsonl` — ข้อกฎหมาย values no rule matched (≈2%, long tail)

```sql
-- text search
SELECT r.doc_no, r.date, r.subject FROM rulings_fts f JOIN rulings r ON r.id = f.id
WHERE rulings_fts MATCH '"อาหารสัตว์"' ORDER BY r.date DESC;
-- every ruling on มาตรา 65 ตรี (13)
SELECT r.doc_no, r.subject FROM ruling_section s JOIN rulings r ON r.id = s.ruling_id
WHERE s.ref = '65 ตรี (13)' AND s.source = 'law';
```

## Semantic search (`collector/embed.py`)

`text-embedding-3-large`, 1024 dims (set in `.env`). Each ruling → a header chunk (เรื่อง + ข้อกฎหมาย)
plus ข้อหารือ / แนววินิจฉัย windows of ~1,500 chars. Output, committed: `data/chunks.jsonl`,
`data/embeddings.npy` (float16), `data/embeddings.json`. Hash-cached: re-runs only embed new text.

## MCP server (`server/`)

- `server/search.py` — hybrid search (FTS + cosine, reciprocal-rank fusion; text search leads only for
  citation-style queries) and `related()` graph neighbours. CLI: `python3 server/search.py "คำถาม"`.
- `server/app.py` — MCP over streamable HTTP at `/mcp` (bearer token), `/health` open.
  Tools: `search_tax_rulings`, `get_tax_ruling`, `rulings_by_law`, `related_rulings`.
  Env: `RD_TOKENS="name:token,..."` (or `MCP_AUTH_TOKEN`), `OPENAI_API_KEY`.
- `Dockerfile` + `render.yaml` — the image builds `rulings.db` from `data/` at build time.
- `skill/rd-tax-rulings/SKILL.md` — how an AI should research with the tools.

Monthly: `collector/update.sh`, then commit `data/` and push → Render redeploys.

## Search quality (`eval/`)

`python3 eval/run.py` — hit@5 and MRR over `eval/queries.jsonl` (query + expected เลขที่หนังสือ).
Add real questions there and re-run before and after any search change. Needs PyThaiNLP installed
(sentence queries are cut into Thai words for the text index): 8/8 with it, 5/8 without.

## Automation

`.github/workflows/monthly-update.yml` runs on the 10th of each month (and on demand): crawl this and
last year → parse (merges into the existing records; `raw/` is not in git) → embed new text → commit
`data/` → Render redeploys. Repo secret: `OPENAI_API_KEY`.
