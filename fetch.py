"""MeeNews fetcher (runs on GitHub Actions).

Commands:
    python fetch.py run --data DIR   fetch enabled sources and merge them into DIR
                                     (the contents of the `data` branch)
    python fetch.py check            test every source and write docs/source-check.md

Articles are stored in their original language. Translation happens in the
reader's browser (see PLAN.md 6.5).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
import yaml

USER_AGENT = "MeeNews/0.1 (personal news reader; +https://github.com/mee6262)"
ACCEPT = "application/rss+xml, application/rdf+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
TIMEOUT = 20
MAX_ITEMS = 50
RETENTION_DAYS = int(os.environ.get("NEWS_RETENTION_DAYS", "30"))
SUMMARY_MAX = 800
CONTENT_MAX = 30000
STALE_HOURS = 48

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
}


class FetchError(Exception):
    """A source could not be fetched or parsed."""


# ---------- helpers ----------

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def make_id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > limit // 2 else limit].rstrip() + "…"


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ---------- sources ----------

def load_sources(path: str | Path, include_disabled: bool = False) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sources = data.get("sources") or []
    return [s for s in sources if include_disabled or s.get("enabled", True)]


def http_get(url: str) -> bytes:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": ACCEPT}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise FetchError(f"{type(e).__name__}: {str(e)[:200]}") from e
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code}")
    return r.content


# ---------- RSS / Atom ----------

def entry_image(entry) -> str | None:
    for m in entry.get("media_content") or []:
        url = m.get("url")
        if not url:
            continue
        kind = (m.get("medium") or m.get("type") or "").lower()
        if kind.startswith("image") or urlparse(url).path.lower().endswith(IMAGE_EXT):
            return url
    for m in entry.get("media_thumbnail") or []:
        if m.get("url"):
            return m["url"]
    for e in entry.get("enclosures") or []:
        if (e.get("type") or "").startswith("image") and e.get("href"):
            return e["href"]
    return None


def entry_content(entry) -> str:
    """Full text if the feed carries it (content:encoded), else empty."""
    for c in entry.get("content") or []:
        text = clean_text(c.get("value"))
        if text:
            return text
    return ""


def entry_published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def parse_rss(source: dict, content: bytes) -> list[dict]:
    feed = feedparser.parse(content)
    if not feed.entries:
        raise FetchError("no entries" + (f" ({feed.get('bozo_exception')})" if feed.get("bozo") else ""))
    suffix = source.get("title_suffix")
    items = []
    for e in feed.entries[:MAX_ITEMS]:
        link = (e.get("link") or "").strip()
        title = clean_text(e.get("title"))
        if not link or not title:
            continue
        if suffix and title.endswith(suffix):
            title = title[: -len(suffix)].rstrip()
        key = e.get("id") if source.get("id_from") == "guid" else None
        items.append({
            "id": make_id(key or link),
            "url": link,
            "title": title,
            "summary": "" if source.get("no_summary") else truncate(clean_text(e.get("summary")), SUMMARY_MAX),
            "content": truncate(entry_content(e), CONTENT_MAX),
            "image_url": entry_image(e),
            "published_at": iso(entry_published(e)),
        })
    return items


# ---------- Google News sitemap ----------

def parse_news_sitemap(source: dict, content: bytes) -> list[dict]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise FetchError(f"bad XML: {e}") from e
    items = []
    for u in root.findall("sm:url", SITEMAP_NS):
        title = clean_text(u.findtext("news:news/news:title", default="", namespaces=SITEMAP_NS))
        loc = (u.findtext("sm:loc", default="", namespaces=SITEMAP_NS) or "").strip()
        if not title or not loc:
            continue  # category pages etc.
        pub = parse_iso(u.findtext("news:news/news:publication_date", namespaces=SITEMAP_NS))
        image = (u.findtext("image:image/image:loc", namespaces=SITEMAP_NS) or "").strip() or None
        items.append({
            "id": make_id(loc),
            "url": loc,
            "title": title,
            "summary": "",
            "content": "",
            "image_url": image,
            "published_at": iso(pub),
        })
    if not items:
        raise FetchError("no news entries in sitemap")
    items.sort(key=lambda i: i["published_at"] or "", reverse=True)
    return items[:MAX_ITEMS]


PARSERS = {"rss": parse_rss, "news-sitemap": parse_news_sitemap}


def fetch_source(source: dict) -> list[dict]:
    parser = PARSERS.get(source.get("type", "rss"))
    if parser is None:
        raise FetchError(f"unknown type {source.get('type')!r}")
    items = parser(source, http_get(source["url"]))
    for it in items:
        it["source_id"] = source["id"]
        it["category"] = source.get("category", "")
        it["lang"] = source.get("lang", "")
    return items


# ---------- run: merge into the data branch ----------

def load_articles(news_dir: Path) -> dict[str, dict]:
    articles: dict[str, dict] = {}
    for p in sorted(news_dir.glob("*.json")):
        for a in read_json(p) or []:
            articles[a["id"]] = a
    return articles


def set_output(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def cmd_run(args) -> int:
    started = time.monotonic()
    run_at = now_utc()
    data_dir = Path(args.data)
    news_dir = data_dir / "news"
    news_dir.mkdir(parents=True, exist_ok=True)

    articles = load_articles(news_dir)
    before = len(articles)
    prev_status = read_json(data_dir / "status.json") or {}
    prev_src = {s["id"]: s for s in prev_status.get("sources", [])}

    src_status = []
    for s in load_sources(args.sources):
        st = {
            "id": s["id"], "name": s["name"], "category": s.get("category", ""), "lang": s.get("lang", ""),
            "ok": False, "count": 0, "new": 0, "error": None,
            "last_ok_at": prev_src.get(s["id"], {}).get("last_ok_at"),
        }
        try:
            items = fetch_source(s)
        except Exception as e:  # noqa: BLE001 - one bad source must not stop the run
            st["error"] = str(e) if isinstance(e, FetchError) else f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[FAIL] {s['name']}: {st['error']}", flush=True)
            src_status.append(st)
            continue
        st.update(ok=True, count=len(items), last_ok_at=iso(run_at))
        for it in items:
            if it["id"] not in articles:
                it["fetched_at"] = iso(run_at)
                articles[it["id"]] = it
                st["new"] += 1
        print(f"[ OK ] {s['name']}: {st['count']} items, {st['new']} new", flush=True)
        src_status.append(st)

    # Group by fetch date and drop anything past retention.
    cutoff = run_at.date() - timedelta(days=RETENTION_DAYS)
    by_day: dict[str, list[dict]] = {}
    for a in articles.values():
        day = a["fetched_at"][:10]
        if date.fromisoformat(day) > cutoff:
            by_day.setdefault(day, []).append(a)

    changed = False
    index_files = []
    for day in sorted(by_day, reverse=True):
        items = sorted(by_day[day], key=lambda a: (a.get("published_at") or a["fetched_at"], a["id"]), reverse=True)
        path = news_dir / f"{day}.json"
        text = dump(items)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed = True
        index_files.append({
            "name": f"news/{day}.json",
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "count": len(items),
        })
    wanted = {f"{day}.json" for day in by_day}
    for p in news_dir.glob("*.json"):
        if p.name not in wanted:
            p.unlink()
            changed = True

    index_path = data_dir / "index.json"
    if changed or not index_path.exists():
        index_path.write_text(json.dumps({
            "generated_at": iso(run_at),
            "retention_days": RETENTION_DAYS,
            "files": index_files,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        changed = True

    ok = sum(1 for s in src_status if s["ok"])
    new_total = sum(s["new"] for s in src_status)
    (data_dir / "status.json").write_text(json.dumps({
        "run_at": iso(run_at),
        "duration_s": round(time.monotonic() - started, 1),
        "sources_ok": ok,
        "sources_total": len(src_status),
        "new_articles": new_total,
        "articles_total": sum(f["count"] for f in index_files),
        "articles_before": before,
        "sources": src_status,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    set_output("changed", "true" if changed else "false")
    print(f"sources ok {ok}/{len(src_status)}, new {new_total}, total {sum(f['count'] for f in index_files)}, changed={changed}")
    return 0 if ok else 1


# ---------- check: test every source ----------

def check_source(source: dict) -> dict:
    res = {
        "id": source["id"], "name": source["name"], "url": source["url"],
        "enabled": source.get("enabled", True), "status": "FAIL", "notes": [],
    }
    started = time.monotonic()
    try:
        items = fetch_source(source)
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e) if isinstance(e, FetchError) else f"{type(e).__name__}: {str(e)[:200]}"
        return res
    res["elapsed_s"] = round(time.monotonic() - started, 2)
    n = len(items)
    res["items"] = n
    res["with_summary"] = sum(1 for i in items if i["summary"])
    res["with_content"] = sum(1 for i in items if i["content"])
    res["with_image"] = sum(1 for i in items if i["image_url"])
    res["unique_ids"] = len({i["id"] for i in items})
    res["sample_title"] = items[0]["title"] if items else None
    dates = [d for d in (parse_iso(i["published_at"]) for i in items) if d]
    if dates:
        res["age_hours"] = round((now_utc() - max(dates)).total_seconds() / 3600, 1)
    else:
        res["notes"].append("no dates")
    if res["unique_ids"] < n:
        res["notes"].append(f"duplicate ids ({n - res['unique_ids']})")
    if res.get("age_hours", 0) > STALE_HOURS:
        res["notes"].append(f"newest item {res['age_hours']}h old")
    res["status"] = "WARN" if res["notes"] else "OK"
    return res


def _cell(value) -> str:
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def write_report(results: list[dict], path: Path) -> None:
    stamp = now_utc().strftime("%Y-%m-%d %H:%M UTC")
    counts = {k: sum(1 for r in results if r["status"] == k) for k in ("OK", "WARN", "FAIL")}
    lines = [
        "# Source check",
        "",
        f"Run: {stamp} (GitHub Actions)  ",
        f"OK {counts['OK']} · WARN {counts['WARN']} · FAIL {counts['FAIL']} · total {len(results)}",
        "",
        "| Source | Enabled | Result | Items | Summary | Full text | Image | Newest (h ago) | Notes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        n = r.get("items")
        frac = (lambda k: f"{r[k]}/{n}") if n else (lambda k: "-")
        notes = r.get("error") or "; ".join(r["notes"])
        lines.append("| " + " | ".join(_cell(x) for x in [
            r["name"], "yes" if r["enabled"] else "no", r["status"], n,
            frac("with_summary"), frac("with_content"), frac("with_image"), r.get("age_hours"), notes,
        ]) + " |")
    lines += ["", "## Sample titles", ""]
    lines += [f"- **{_cell(r['name'])}**: {_cell(r['sample_title'])}" for r in results if r.get("sample_title")]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_check(args) -> int:
    results = []
    for s in load_sources(args.sources, include_disabled=True):
        r = check_source(s)
        print(f"[{r['status']:4}] {s['name']}: {r.get('error') or '; '.join(r['notes']) or 'ok'}", flush=True)
        results.append(r)
    write_report(results, Path(args.out))
    Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="MeeNews fetcher")
    p.add_argument("--sources", default="sources.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch enabled sources and merge into the data dir")
    r.add_argument("--data", default="data")
    r.set_defaults(func=cmd_run)
    c = sub.add_parser("check", help="test every source and write a report")
    c.add_argument("--out", default="docs/source-check.md")
    c.add_argument("--json", default="source-check.json")
    c.set_defaults(func=cmd_check)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
