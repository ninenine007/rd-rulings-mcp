---
name: rd-tax-rulings
description: Research Thai tax questions against the Revenue Department's ข้อหารือภาษีอากร (2540–present) through the rd-rulings MCP tools. Use whenever a lawyer asks how the Revenue Department treats a transaction, whether something is taxable/exempt/deductible, for precedent rulings on a มาตรา, or names a เลขที่หนังสือ (กค 07xx/…).
---

# ข้อหารือภาษีอากร research

Tools: `search_tax_rulings`, `get_tax_ruling`, `rulings_by_law`, `related_rulings`.

## Workflow
1. **Frame the issue** in one line: tax (VAT, CIT, PIT, WHT, SBT, stamp duty), the transaction, the provision if known.
2. **Search 2–4 times**, each phrasing different:
   - the facts in plain Thai ("บริษัทให้ที่ดินแก่พนักงานโดยไม่มีค่าตอบแทน")
   - the legal concept ("รายจ่ายต้องห้าม ค่ารับรอง")
   - the provision as a filter (`section="65 ตรี (13)"`) or via `rulings_by_law`
   Add `tax_type` / `year_from` when the question is clearly scoped. Recent law → `year_from` ≈ the last amendment.
3. **Read before relying**: `get_tax_ruling` on every ruling you will cite. The snippet is not the ruling.
4. **Widen from the best hit** with `related_rulings` — same provisions, or rulings citing each other.
5. **Answer**:
   - lead with the Department's position in one or two sentences;
   - cite each ruling: เลขที่หนังสือ, date, one line on its facts, its url;
   - flag conflicts between rulings and which is later;
   - flag that a ruling binds only its own facts and may predate an amendment (check the date);
   - say plainly when nothing on point was found — never fill the gap from memory.

## Notes
- One เลขที่หนังสือ can recur across years; if `get_tax_ruling` returns several, pick by date/subject.
- Years can be given in พ.ศ. or ค.ศ.
- Coverage: rulings published on rd.go.th; a few pages are broken on the site itself.
