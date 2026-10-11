"""Collect ข้อหารือภาษีอากร from www.rd.go.th into a local archive.

  python3 rd_rulings.py crawl [--years 2569 2568 ...] [--delay 1.5]
  python3 rd_rulings.py parse

crawl  : year page -> month pages -> every listing page -> each ruling.
         Raw HTML lands in raw/<page-id>.html; already-saved rulings are
         skipped, so re-running only fetches what is new. index.jsonl
         records where each ruling was listed (year, month, url).
parse  : reads raw/ + index.jsonl, writes data/rulings.jsonl (one ruling
         per line) and data/parse_errors.jsonl for pages that did not parse.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.rd.go.th"
START = "/68689.html"  # any ข้อหารือ page; its sidebar lists every year
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RAW, DATA = HERE / "raw", ROOT / "data"
INDEX = HERE / "index.jsonl"

MONTHS = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
          "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
FIELDS = {"เลขที่หนังสือ": "doc_no", "วันที่": "date_th", "เรื่อง": "subject",
          "ข้อกฎหมาย": "law", "ข้อหารือ": "question", "แนววินิจฉัย": "ruling",
          "เลขตู้": "cabinet_no", "ลงวันที่": "date_th", "ประเด็นปัญหา": "question",
          "มาตรา": "law", "แนววินิจฉั": "ruling"}  # variants seen on older pages

session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0 (office research archive; polite crawler)"


def fetch(path, delay):
    url = path if path.startswith("http") else BASE + "/" + path.lstrip("/")
    for attempt in range(4):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            time.sleep(delay)
            return r.content
        except requests.RequestException as e:
            wait = delay * (2 ** (attempt + 1))
            print(f"  retry {attempt + 1} {url}: {e}", file=sys.stderr)
            time.sleep(wait)
    print(f"  GAVE UP {url}", file=sys.stderr)
    return None


def soup(content):
    content = content or b""
    return BeautifulSoup(content, "html.parser", from_encoding="utf-8")


def page_id(href):
    m = re.search(r"(\d+)\.html", href)
    return m.group(1) if m else None


def main_anchors(s):
    main = s.find("main") or s
    return [(a.get_text(strip=True), a.get("href", "")) for a in main.find_all("a")]


def year_links(delay):
    s = soup(fetch(START, delay))
    seen = {}
    for text, href in main_anchors(s):
        if re.fullmatch(r"25\d\d", text) and text not in seen:
            seen[text] = href
    return seen


def month_links(year_href, delay):
    s = soup(fetch(year_href, delay))
    out = {}
    for text, href in main_anchors(s):
        if text in MONTHS and page_id(href):
            out.setdefault(text, href)
    return out


def listing(month_href, delay):
    """Return ruling hrefs from every page of a month's listing."""
    rulings, pages, todo, numbered = [], set(), [month_href], 0
    while todo:
        href = todo.pop(0)
        if page_id(href) in pages:
            continue
        pages.add(page_id(href))
        s = soup(fetch(href, delay))
        table = (s.find("main") or s).find("table")
        if not table:
            continue
        for n in re.findall(r"(?:^|\s)(\d{1,3})\.\s", table.get_text(" ")):
            numbered = max(numbered, int(n))  # the listing numbers its items 1., 2., ...
        for a in table.find_all("a"):
            h, t = a.get("href", ""), a.get_text(strip=True)
            if not page_id(h):
                continue
            if t.isdigit():
                todo.append(h)  # pagination
            elif t and t not in MONTHS and not re.fullmatch(r"25\d\d", t):
                rulings.append(h)
    rulings = list(dict.fromkeys(rulings))
    if numbered and numbered != len(rulings):
        print(f"  CHECK {month_href}: numbered to {numbered}, found {len(rulings)} links",
              file=sys.stderr)
    return rulings


def crawl(args):
    RAW.mkdir(exist_ok=True)
    indexed = set()
    if INDEX.exists():
        indexed = {json.loads(l)["id"] for l in INDEX.open(encoding="utf-8")}
    years = year_links(args.delay)
    wanted = args.years or sorted(years, reverse=True)
    with INDEX.open("a", encoding="utf-8") as idx:
        for y in wanted:
            if y not in years:
                print(f"year {y} not found", file=sys.stderr)
                continue
            months = month_links(years[y], args.delay)
            for m in [m for m in MONTHS if m in months]:
                if args.months and m not in args.months:
                    continue
                hrefs = listing(months[m], args.delay)
                new = 0
                for h in hrefs:
                    pid = page_id(h)
                    f = RAW / f"{pid}.html"
                    if not f.exists():
                        content = fetch(h, args.delay)
                        if content is None:
                            continue  # not indexed, so the next run retries it
                        f.write_bytes(content)
                        new += 1
                    if pid not in indexed:
                        idx.write(json.dumps({"id": pid, "year": y, "month": m,
                                              "url": f"{BASE}/{pid}.html"},
                                             ensure_ascii=False) + "\n")
                        indexed.add(pid)
                idx.flush()
                print(f"{y} {m}: {len(hrefs)} listed, {new} new", flush=True)


HARD, LABEL = "\x01", "\x00"  # paragraph break / label marker


def paragraphs(text):
    """Older pages hard-wrap a printed letter with <br>; a real paragraph starts
    after a <p>, at an &nbsp; indent, or at a list marker, so join the rest."""
    paras = []
    for raw in text.split("\n"):
        hard = HARD in raw
        raw = raw.replace(HARD, "")
        line = re.sub(r"[ \t\xa0]+", " ", raw).strip()
        if not line:
            continue
        starts_para = hard or raw.lstrip(" \t").startswith("\xa0") or re.match(
            r"(\d+(\.\d+)*\.(?!\d)|\(\d+\)|[ก-ฮ]\.\s|\([ก-ฮ]\))", line)
        if paras and not starts_para:
            paras[-1] += " " + line
        else:
            paras.append(line)
    return re.sub(r"^:\s*", "", "\n".join(paras)).strip()


def thai_date_iso(s):
    m = re.search(r"(\d{1,2})\s*(\S+?)\s*(\d{4})", s or "")
    if not m or m.group(2) not in MONTHS:
        return None
    return f"{int(m.group(3)) - 543:04d}-{MONTHS.index(m.group(2)) + 1:02d}-{int(m.group(1)):02d}"


def parse_ruling(content):
    # The markup is often broken (missing <tr>, text outside its <td>), so read
    # the whole content block in order and split it at the field labels.
    s = soup(content)
    table = (s.find("main") or s).find("table")
    if not table:
        return {}
    block = table.find_parent("div") or table
    for t in block.find_all(string=True):
        t.replace_with(re.sub(r"\s*\n\s*", " ", t))  # source wrapping, not breaks
    for tag in block.find_all(["strong", "b"]):
        label = tag.get_text(strip=True).rstrip(":").strip()
        if label in FIELDS:
            tag.replace_with(f"\n{LABEL}{FIELDS[label]}{LABEL}\n")
    for tag in block.find_all(["p", "td", "tr"]):
        tag.insert_after(f"\n{HARD}")
    for br in block.find_all("br"):
        br.replace_with("\n")
    parts = block.get_text().split(LABEL)
    rec = {}
    for key, value in zip(parts[1::2], parts[2::2]):
        if key not in rec:
            rec[key] = paragraphs(value)
    for key in ("doc_no", "date_th", "subject", "cabinet_no"):  # one-line fields
        if key in rec:
            rec[key] = re.sub(r"\s+", " ", rec[key]).strip()
    return rec


def parse(args):
    DATA.mkdir(exist_ok=True)
    meta = {}
    if INDEX.exists():
        for l in INDEX.open(encoding="utf-8"):
            d = json.loads(l)
            meta[d["id"]] = d
    ok = bad = 0
    with (DATA / "rulings.jsonl").open("w", encoding="utf-8") as out, \
         (DATA / "parse_errors.jsonl").open("w", encoding="utf-8") as err:
        for f in sorted(RAW.glob("*.html"), key=lambda p: int(p.stem)):
            rec = parse_ruling(f.read_bytes())
            m = meta.get(f.stem, {"id": f.stem, "url": f"{BASE}/{f.stem}.html"})
            missing = [k for k in ("doc_no", "subject") if not rec.get(k)]
            if missing:
                err.write(json.dumps({**m, "missing": missing}, ensure_ascii=False) + "\n")
                bad += 1
                continue
            empty = [k for k in ("question", "ruling") if not rec.get(k)]
            if empty:
                rec["incomplete"] = empty  # blank on the source page itself
            rec["date"] = thai_date_iso(rec.get("date_th"))
            rec["tax_type"] = re.split(r"\s*(?:กรณี|:)", rec["subject"], maxsplit=1)[0].strip()
            out.write(json.dumps({"id": m["id"], "listed_year": m.get("year"),
                                  "listed_month": m.get("month"), "url": m["url"], **rec},
                                 ensure_ascii=False) + "\n")
            ok += 1
    print(f"parsed {ok}, errors {bad}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crawl")
    c.add_argument("--years", nargs="*")
    c.add_argument("--months", nargs="*")
    c.add_argument("--delay", type=float, default=1.5)
    sub.add_parser("parse")
    a = p.parse_args()
    {"crawl": crawl, "parse": parse}[a.cmd](a)
