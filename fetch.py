"""MeeNews fetcher: pull RSS feeds and translate them to Thai.

Phase 0 command:
    python fetch.py check   -> test every source in sources.yaml and write a report

The scheduled fetch run (writing to the `data` branch) is added in phase 1.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
import yaml
from deep_translator import GoogleTranslator

USER_AGENT = "MeeNews/0.1 (personal news reader; +https://github.com/mee6262)"
ACCEPT = "application/rss+xml, application/rdf+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
TIMEOUT = 20
MAX_ITEMS = 50
TRANSLATE_DELAY = 1.0
CHUNK_CHARS = 4500
STALE_HOURS = 48

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")


# ---------- sources ----------

def load_sources(path: str | Path, include_disabled: bool = False) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sources = data.get("sources") or []
    return [s for s in sources if include_disabled or s.get("enabled", True)]


# ---------- feed parsing ----------

def fetch_feed(url: str) -> requests.Response:
    return requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": ACCEPT}, timeout=TIMEOUT)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def entry_id(source: dict, entry) -> str:
    key = entry.get("id") if source.get("id_from") == "guid" else None
    key = key or entry.get("link") or entry.get("id") or entry.get("title", "")
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


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


# ---------- translation ----------

_translator: GoogleTranslator | None = None


def split_chunks(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    chunks, buf = [], ""
    for para in text.split("\n"):
        while len(para) > limit:  # very long paragraph: hard split
            chunks.append(para[:limit])
            para = para[limit:]
        if len(buf) + len(para) + 1 > limit and buf:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


def translate(text: str) -> str:
    """Translate to Thai. Raises on failure so callers can set needs_translation."""
    global _translator
    if not text:
        return text
    if _translator is None:
        _translator = GoogleTranslator(source="auto", target="th")
    out = []
    for chunk in split_chunks(text):
        result = _translator.translate(chunk)
        if not result:
            raise RuntimeError("empty translation")
        out.append(result)
        time.sleep(TRANSLATE_DELAY)
    return "\n".join(out)


# ---------- phase 0: source check ----------

def check_source(source: dict) -> dict:
    res = {
        "id": source["id"],
        "name": source["name"],
        "url": source["url"],
        "category": source.get("category", ""),
        "enabled": source.get("enabled", True),
        "status": "FAIL",
        "notes": [],
    }
    started = time.monotonic()
    try:
        r = fetch_feed(source["url"])
    except requests.RequestException as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return res
    res["http"] = r.status_code
    res["elapsed_s"] = round(time.monotonic() - started, 2)
    if r.url != source["url"]:
        res["final_url"] = r.url
    if r.status_code != 200:
        res["error"] = f"HTTP {r.status_code}"
        return res

    feed = feedparser.parse(r.content)
    entries = feed.entries[:MAX_ITEMS]
    if not entries:
        res["error"] = "no entries" + (f" ({feed.get('bozo_exception')})" if feed.get("bozo") else "")
        return res

    n = len(entries)
    res["items"] = len(feed.entries)
    res["with_summary"] = sum(1 for e in entries if clean_text(e.get("summary")))
    res["with_content"] = sum(1 for e in entries if entry_content(e))
    res["with_image"] = sum(1 for e in entries if entry_image(e))
    res["unique_ids"] = len({entry_id(source, e) for e in entries})
    res["link_host"] = urlparse(entries[0].get("link", "")).netloc

    dates = [d for d in (entry_published(e) for e in entries) if d]
    if dates:
        newest = max(dates)
        res["newest"] = newest.isoformat()
        res["age_hours"] = round((datetime.now(timezone.utc) - newest).total_seconds() / 3600, 1)
    else:
        res["notes"].append("no dates")

    title = clean_text(entries[0].get("title"))
    res["sample_title"] = title
    try:
        res["sample_th"] = translate(title)
        res["translate"] = "OK"
    except Exception as e:  # noqa: BLE001 - report any translator failure
        res["translate"] = f"FAIL: {type(e).__name__}: {str(e)[:150]}"
        res["notes"].append("translation failed")

    if res["unique_ids"] < n:
        res["notes"].append(f"duplicate ids ({n - res['unique_ids']})")
    if res.get("age_hours", 0) > STALE_HOURS:
        res["notes"].append(f"newest item {res['age_hours']}h old")
    if res["with_summary"] == 0:
        res["notes"].append("no summaries")
    res["status"] = "WARN" if res["notes"] else "OK"
    return res


def _cell(value) -> str:
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def write_report(results: list[dict], path: Path) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    counts = {k: sum(1 for r in results if r["status"] == k) for k in ("OK", "WARN", "FAIL")}
    lines = [
        "# Source check",
        "",
        f"Run: {now} (GitHub Actions)  ",
        f"OK {counts['OK']} · WARN {counts['WARN']} · FAIL {counts['FAIL']} · total {len(results)}",
        "",
        "| Source | Enabled | Result | HTTP | Items | Summary | Full text | Image | Newest (h ago) | Translate | Notes |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        sample = min(r.get("items", 0), MAX_ITEMS)
        frac = lambda k: f"{r[k]}/{sample}" if k in r else "-"  # noqa: E731
        notes = "; ".join(r["notes"]) or ""
        if r.get("error"):
            notes = r["error"]
        if r.get("final_url"):
            notes += f" (redirect → {r['final_url']})"
        lines.append("| " + " | ".join(_cell(x) for x in [
            r["name"], "yes" if r["enabled"] else "candidate", r["status"], r.get("http"),
            r.get("items"), frac("with_summary"), frac("with_content"), frac("with_image"),
            r.get("age_hours"), (r.get("translate") or "-").split(":")[0], notes,
        ]) + " |")

    lines += ["", "## Sample translations", ""]
    for r in results:
        if r.get("sample_title"):
            lines.append(f"- **{_cell(r['name'])}**: {_cell(r['sample_title'])}  ")
            lines.append(f"  → {_cell(r.get('sample_th') or r.get('translate'))}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_check(args) -> int:
    sources = load_sources(args.sources, include_disabled=True)
    results = []
    for s in sources:
        r = check_source(s)
        print(f"[{r['status']:4}] {s['name']}: {r.get('error') or '; '.join(r['notes']) or 'ok'}", flush=True)
        results.append(r)
    write_report(results, Path(args.out))
    Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="MeeNews fetcher")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="test every source and write a report")
    c.add_argument("--sources", default="sources.yaml")
    c.add_argument("--out", default="docs/source-check.md")
    c.add_argument("--json", default="source-check.json")
    c.set_defaults(func=cmd_check)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
