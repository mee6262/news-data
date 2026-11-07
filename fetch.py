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
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

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
_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad]")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = _INVISIBLE_RE.sub("", text)
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


PAGE_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"


def http_get(url: str, accept: str = ACCEPT) -> bytes:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": accept}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise FetchError(f"{type(e).__name__}: {str(e)[:200]}") from e
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code}")
    return r.content


# ---------- RSS / Atom ----------

IMAGE_PLACEHOLDERS = ("bangkokpost.com/v3/assets/images/img-default",)


def entry_image(entry) -> str | None:
    url = _entry_image(entry)
    return None if url and any(p in url for p in IMAGE_PLACEHOLDERS) else url


def _entry_image(entry) -> str | None:
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
        content = truncate(entry_content(e), CONTENT_MAX)
        image = entry_image(e)
        item = {
            "id": make_id(key or link),
            "url": link,
            "title": title,
            "summary": "" if source.get("no_summary") else truncate(clean_text(e.get("summary")), SUMMARY_MAX),
            "content": content,
            "image_url": image,
            "published_at": iso(entry_published(e)),
        }
        if content:  # full text came with the feed: take its images/videos from the same HTML
            raw = next((c.get("value") for c in e.get("content") or [] if c.get("value")), "")
            media = feed_media(raw, image)
            if not image:
                first = next((m for m in media if m["t"] == "img"), None)
                if first:
                    item["image_url"] = first["src"]
                    media.remove(first)
            item["media"] = media
            item["media_v"] = MEDIA_VERSION
        items.append(item)
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
        if image and "media.nationthailand.com/" in image and "/uploads/images/" not in image:
            # the sitemap drops the path prefix; the real file lives under /uploads/images/md/
            image = re.sub(r"media\.nationthailand\.com/(\d{4}/\d{2}/)", r"media.nationthailand.com/uploads/images/md/\1", image)
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


# ---------- full text from article pages (phase 3) ----------

FULLTEXT_MAX_PER_RUN = int(os.environ.get("FULLTEXT_MAX_PER_RUN", "120"))
FULLTEXT_BUDGET_S = int(os.environ.get("FULLTEXT_BUDGET_S", "150"))
FULLTEXT_WORKERS = 10      # different hosts in parallel; one host is always sequential
HOST_DELAY_S = 1.5         # pause between pages on the same host
CONTENT_MIN = 400          # shorter extractions are treated as failures (paywall, video page...)
ROBOTS_AGENT = "MeeNews"


def _robots_for(host: str) -> RobotFileParser | None:
    """robots.txt for https://host. None means 'allow all'."""
    rp = RobotFileParser()
    try:
        r = requests.get(f"https://{host}/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code in (404, 410):
        return None
    if r.status_code != 200:
        rp.disallow_all = True
        return rp
    rp.parse(r.text.splitlines())
    return rp


# ---------- images and videos inside articles ----------

MEDIA_VERSION = 2          # bump to re-scan pages when the media rules change
DW_MEDIA_VERSION = 3       # DW pages got their own reader (images + HLS videos)


def media_version_for(a: dict) -> int:
    return DW_MEDIA_VERSION if urlparse(a.get("url", "")).netloc.endswith("dw.com") else MEDIA_VERSION
MAX_IMAGES = 8
_IMG_SKIP = re.compile(r"(logo|icon|avatar|placeholder|spacer|pixel|1x1|blank|sprite|badge|byline)", re.I)
_YT = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")
_OG_VIDEO = re.compile(r'<meta[^>]+property=["\']og:video(?::secure_url|:url)?["\'][^>]+content=["\']([^"\']+)', re.I)
_FEED_IMG = re.compile(r'<img[^>]+src=["\'](https://[^"\']+)["\']', re.I)


_SIZE_TOKENS = re.compile(r"(/(?:standard|ws)/\d+/|/w:\d+/|/w\d{3,4}/|_\d{3,4}(?=\.\w+$)|/\d+x\d+/|/(?:md|lg|sm)/)")


def _img_key(url: str) -> str:
    """Same picture at different sizes -> same key (BBC /standard/976/, Bangkok Post _800, France 24 w:1024...)."""
    return _SIZE_TOKENS.sub("/", urlparse(url).path.lower())


def _same_image(a: str | None, b: str | None) -> bool:
    return bool(a and b) and _img_key(a) == _img_key(b)


def _good_image(src: str | None, cover: str | None) -> bool:
    if not src or not src.startswith("https://") or len(src) > 600:
        return False
    path = urlparse(src).path.lower()
    if path.endswith((".svg", ".gif")) or _IMG_SKIP.search(path) or any(p in src for p in IMAGE_PLACEHOLDERS):
        return False
    return not _same_image(src, cover)


def _block_text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def extract_article(trafilatura, html_text: str, url: str, cover: str | None) -> tuple[list[str], list[dict]]:
    """Paragraphs and in-text images (with the paragraph index they follow) from a page."""
    xml = trafilatura.extract(html_text, url=url, output_format="xml", include_images=True,
                              include_comments=False, include_tables=False, favor_precision=True)
    if not xml:
        return [], []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        text = trafilatura.extract(html_text, url=url, include_comments=False, include_tables=False,
                                   favor_precision=True) or ""
        return [line.strip() for line in text.splitlines() if line.strip()], []
    paras: list[str] = []
    images: list[dict] = []
    seen: set[str] = set()

    def add_image(g) -> None:
        src = (g.get("src") or "").strip()
        key = _img_key(src)
        if len(images) < MAX_IMAGES and key not in seen and _good_image(src, cover):
            seen.add(key)
            images.append({"t": "img", "src": src, "alt": (g.get("alt") or g.get("title") or "")[:200], "pos": len(paras)})

    def walk(el) -> None:
        for child in el:
            if child.tag == "graphic":
                add_image(child)
            elif child.tag in ("p", "head", "item", "quote", "cell"):
                text = _block_text(child)
                if text:
                    paras.append(text)
                for g in child.iter("graphic"):
                    add_image(g)
            else:
                walk(child)

    walk(root.find("main") if root.find("main") is not None else root)
    # drop leading bylines/"- Published" before the first real paragraph, keeping image positions in step
    dropped = 0
    while paras and len(paras[0]) < 40:
        paras.pop(0)
        dropped += 1
    for img in images:
        img["pos"] = max(0, img["pos"] - dropped)
    return paras, images


_DW_STATE = re.compile(r"window\.__APP_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.S)
_DW_TOKENS = re.compile(
    r"(?P<fig><figure[^>]*placeholder-image[^>]*>.*?</figure>)"
    r"|(?P<vid><div[^>]*class=\"[^\"]*vjs-wrapper[^\"]*\"[^>]*>)"
    r"|(?P<blk><(?P<tag>p|h2|h3|li|blockquote)\b[^>]*>.*?</(?P=tag)>)",
    re.S,
)


def dw_extract(html_text: str) -> tuple[list[str], list[dict], str | None] | None:
    """DW pages carry the article as JSON (window.__APP_STATE__): body HTML with
    placeholders for images/videos, plus the media they stand for, in order.
    Returns (paragraphs, media, cover image) or None if the page is not a DW article."""
    m = _DW_STATE.search(html_text)
    if not m:
        return None
    try:
        state = json.loads(m.group(1))
    except ValueError:
        return None
    content = next((v.get("data", {}).get("content") for k, v in state.items()
                    if "/content/article/" in k and isinstance(v, dict)), None)
    if not isinstance(content, dict) or not content.get("text"):
        return None
    links = content.get("contentLinks") or []
    images = [l for l in links if l.get("targetType") == "IMAGE" and l.get("isInText")]
    videos_by_id = {v.get("id"): v for v in content.get("videos") or [] if v.get("hlsVideoSrc")}
    in_text_video_ids = [l.get("targetId") for l in links if l.get("targetType") == "VIDEO" and l.get("isInText")]

    def video(v: dict, pos: int) -> dict | None:
        src = v.get("hlsVideoSrc") or ""
        if not src.startswith("https://"):
            return None
        poster = v.get("posterImageUrl") or ""
        return {"t": "hls", "src": src, "poster": poster if poster.startswith("https://") else "",
                "title": (v.get("title") or "")[:200], "dur": v.get("formattedDuration") or "", "pos": pos}

    paras: list[str] = []
    media: list[dict] = []
    img_i = vid_i = 0
    for tok in _DW_TOKENS.finditer(content["text"]):
        if tok.group("fig"):
            if img_i < len(images):
                link = images[img_i]
                media.append({"t": "img", "src": f"https://static.dw.com/image/{link['targetId']}_6.jpg",
                              "alt": (link.get("description") or link.get("name") or "")[:200], "pos": len(paras)})
            img_i += 1
        elif tok.group("vid"):
            if vid_i < len(in_text_video_ids) and in_text_video_ids[vid_i] in videos_by_id:
                v = video(videos_by_id[in_text_video_ids[vid_i]], len(paras))
                if v:
                    media.append(v)
            vid_i += 1
        else:
            text = clean_text(tok.group("blk")).replace("\n", " ")
            if text:
                paras.append(text)
    # the article's main video (not placed in the text) goes first
    for vid, v in videos_by_id.items():
        if vid not in in_text_video_ids:
            main = video(v, 0)
            if main:
                media.insert(0, main)
    cover_id = (content.get("mainContentImage") or {}).get("id")
    cover = f"https://static.dw.com/image/{cover_id}_6.jpg" if cover_id else None
    return paras, media[: MAX_IMAGES + 3], cover


def page_videos(html_text: str) -> list[dict]:
    """YouTube embeds and og:video mp4 files on a page (filtered later for site-wide promos)."""
    videos, seen = [], set()
    for m in _OG_VIDEO.finditer(html_text):
        src = html.unescape(m.group(1)).strip()
        yt = _YT.search(src)
        if yt and yt.group(1) not in seen:
            seen.add(yt.group(1))
            videos.append({"t": "yt", "id": yt.group(1)})
        elif src.startswith("https://") and urlparse(src).path.lower().endswith(".mp4") and src not in seen:
            seen.add(src)
            videos.append({"t": "mp4", "src": src})
    for m in _YT.finditer(html_text):
        if m.group(1) not in seen and len(videos) < 3:
            seen.add(m.group(1))
            videos.append({"t": "yt", "id": m.group(1)})
    return videos


def feed_media(raw_html: str | None, cover: str | None) -> list[dict]:
    """Images and YouTube embeds inside a feed's content:encoded HTML."""
    if not raw_html:
        return []
    media, seen = [], set()
    for m in _FEED_IMG.finditer(raw_html):
        src = html.unescape(m.group(1))
        if len([x for x in media if x["t"] == "img"]) >= MAX_IMAGES or _img_key(src) in seen or not _good_image(src, cover):
            continue
        seen.add(_img_key(src))
        before = clean_text(raw_html[: m.start()])
        media.append({"t": "img", "src": src, "alt": "", "pos": len(before.split("\n")) if before else 0})
    for m in _YT.finditer(raw_html):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            media.append({"t": "yt", "id": m.group(1)})
    return media


def drop_promo_videos(articles: dict[str, dict]) -> int:
    """A video found on 3+ articles of the same source is a site-wide promo, not news."""
    counts: dict[tuple[str, str], int] = {}
    for a in articles.values():
        for m in a.get("media") or []:
            if m["t"] in ("yt", "mp4"):
                key = (a.get("source_id", ""), m.get("id") or m.get("src"))
                counts[key] = counts.get(key, 0) + 1
    promos = {k for k, n in counts.items() if n >= 3}
    removed = 0
    for a in articles.values():
        media = a.get("media") or []
        kept = [m for m in media if m["t"] == "img" or (a.get("source_id", ""), m.get("id") or m.get("src")) not in promos]
        if len(kept) != len(media):
            removed += len(media) - len(kept)
            a["media"] = kept
    return removed


def _first_paragraph(text: str, limit: int = 320) -> str:
    for line in text.split("\n"):
        if len(line) > 60:
            return truncate(line, limit)
    return truncate(text.replace("\n", " "), limit)


def fill_fulltext(articles: dict[str, dict], sources: dict[str, dict]) -> dict:
    """Fetch article pages for items without content and extract the text with
    trafilatura. Each article is tried once (`fulltext` records the outcome)."""
    import trafilatura  # imported here so `check` does not need it

    def wanted(a: dict) -> bool:
        if not sources.get(a.get("source_id"), {}).get("fulltext", True) or not a.get("url", "").startswith("https://"):
            return False
        if not a.get("content") and not a.get("fulltext"):
            return True  # never tried
        # page already read for text, but before images/videos were collected
        return a.get("fulltext") in ("ok", "empty") and a.get("media_v", 0) < media_version_for(a)

    todo = [a for a in articles.values() if wanted(a)]
    todo.sort(key=lambda a: a["fetched_at"], reverse=True)  # newest first, old ones backfill later
    todo = todo[:FULLTEXT_MAX_PER_RUN]
    by_host: dict[str, list[dict]] = {}
    for a in todo:
        by_host.setdefault(urlparse(a["url"]).netloc, []).append(a)

    deadline = time.monotonic() + FULLTEXT_BUDGET_S
    stats = {"tried": 0, "ok": 0, "empty": 0, "fail": 0, "robots": 0, "images": 0,
             "with_media_images": 0, "with_videos": 0}
    lock = threading.Lock()

    def bump(key: str) -> None:
        with lock:
            stats[key] += 1

    def work(host: str, items: list[dict]) -> None:
        rp = _robots_for(host)
        for i, a in enumerate(items):
            if time.monotonic() > deadline:
                return  # left untouched: tried again next run
            if rp is not None and not rp.can_fetch(ROBOTS_AGENT, a["url"]):
                a["fulltext"] = "robots"
                bump("robots")
                continue
            if i:
                time.sleep(HOST_DELAY_S)
            bump("tried")
            try:
                html_text = http_get(a["url"], PAGE_ACCEPT).decode("utf-8", errors="replace")
            except FetchError:
                a["fulltext"] = "fail"
                bump("fail")
                continue
            had_text = bool(a.get("content"))
            dw = dw_extract(html_text) if host.endswith("dw.com") else None
            if dw:
                paras, media, cover = dw
                if not a.get("image_url") and cover:
                    a["image_url"] = cover
                    bump("images")
                text = "\n".join(paras)
                if len(text) >= CONTENT_MIN:
                    a["content"] = truncate(text, CONTENT_MAX)
                    a["fulltext"] = "ok"
                    bump("ok")
                    if not a.get("summary"):
                        a["summary"] = _first_paragraph(text)
                elif not had_text:
                    a["fulltext"] = "empty"
                    bump("empty")
                a["media"] = [m for m in media if m["t"] != "img" or not _same_image(m["src"], a.get("image_url"))]
                a["media_v"] = media_version_for(a)
                if any(m["t"] == "img" for m in a["media"]):
                    bump("with_media_images")
                if any(m["t"] != "img" for m in a["media"]):
                    bump("with_videos")
                continue
            if not a.get("image_url"):
                meta = trafilatura.extract_metadata(html_text)
                image = getattr(meta, "image", None) if meta else None
                if image and image.startswith("https://") and not any(p in image for p in IMAGE_PLACEHOLDERS):
                    a["image_url"] = image
                    bump("images")
            paras, images = extract_article(trafilatura, html_text, a["url"], a.get("image_url"))
            text = "\n".join(paras)
            if len(text) >= CONTENT_MIN:
                # (re)write the text from the same extraction so image positions line up
                a["content"] = truncate(text, CONTENT_MAX)
                a["fulltext"] = "ok"
                bump("ok")
                if not a.get("summary"):
                    a["summary"] = _first_paragraph(text)
            elif not had_text:
                a["fulltext"] = "empty"
                bump("empty")
            if not a.get("content"):
                images = [dict(img, pos=0) for img in images]  # no text to place them in
            media = images + page_videos(html_text)
            a["media"] = media
            a["media_v"] = media_version_for(a)
            if images:
                bump("with_media_images")
            if any(m["t"] != "img" for m in media):
                bump("with_videos")

    with ThreadPoolExecutor(max_workers=FULLTEXT_WORKERS) as pool:
        for future in [pool.submit(work, h, items) for h, items in by_host.items()]:
            future.result()
    stats["queued"] = len(todo)
    stats["promo_videos_removed"] = drop_promo_videos(articles)
    return stats


# ---------- run: merge into the data branch ----------

def load_articles(news_dir: Path) -> dict[str, dict]:
    articles: dict[str, dict] = {}
    for p in sorted(news_dir.glob("*.json")):
        for a in read_json(p) or []:
            articles[a["id"]] = a
    return articles


# Broad categories: a story there moves to a section category when a section feed also carries it.
GENERAL_CATEGORIES = {"world", "th", ""}


def recategorize(articles: dict[str, dict], sources: list[dict]) -> int:
    """Stored stories follow their source when its category is changed in sources.yaml."""
    cat_of = {s["id"]: s.get("category", "") for s in sources}
    changed = 0
    for a in articles.values():
        new = cat_of.get(a.get("source_id"))
        cur = a.get("category", "")
        if new is None or new == cur:
            continue
        # a section category the story got from a second feed is kept (unless the source itself is specific)
        if cur not in GENERAL_CATEGORIES and new in GENERAL_CATEGORIES:
            continue
        a["category"] = new
        changed += 1
    if changed:
        print(f"recategorized {changed} stored stories", flush=True)
    return changed


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
    recategorize(articles, load_sources(args.sources, include_disabled=True))
    prev_status = read_json(data_dir / "status.json") or {}
    prev_src = {s["id"]: s for s in prev_status.get("sources", [])}

    def try_fetch(source: dict):
        try:
            return fetch_source(source), None
        except Exception as e:  # noqa: BLE001 - one bad source must not stop the run
            return None, str(e) if isinstance(e, FetchError) else f"{type(e).__name__}: {str(e)[:200]}"

    sources = load_sources(args.sources)
    with ThreadPoolExecutor(max_workers=10) as pool:  # feeds are on different hosts: fetch them together
        fetched = list(pool.map(try_fetch, sources))

    src_status = []
    for s, (items, error) in zip(sources, fetched):
        st = {
            "id": s["id"], "name": s["name"], "category": s.get("category", ""), "lang": s.get("lang", ""),
            "ok": False, "count": 0, "new": 0, "error": None,
            "last_ok_at": prev_src.get(s["id"], {}).get("last_ok_at"),
        }
        if error is not None:
            st["error"] = error
            print(f"[FAIL] {s['name']}: {st['error']}", flush=True)
            src_status.append(st)
            continue
        st.update(ok=True, count=len(items), last_ok_at=iso(run_at))
        for it in items:
            if it["id"] not in articles:
                it["fetched_at"] = iso(run_at)
                articles[it["id"]] = it
                st["new"] += 1
            else:
                # same story in a broad feed and a section feed (e.g. Matichon + Matichon Sport): keep the section
                old_cat = articles[it["id"]].get("category", "")
                if old_cat in GENERAL_CATEGORIES and it["category"] not in GENERAL_CATEGORIES:
                    articles[it["id"]]["category"] = it["category"]
        print(f"[ OK ] {s['name']}: {st['count']} items, {st['new']} new", flush=True)
        src_status.append(st)

    fulltext_stats = None
    if os.environ.get("FULLTEXT", "1") == "1":
        sources_by_id = {s["id"]: s for s in load_sources(args.sources, include_disabled=True)}
        t0 = time.monotonic()
        fulltext_stats = fill_fulltext(articles, sources_by_id)
        fulltext_stats["seconds"] = round(time.monotonic() - t0, 1)
        print(f"full text: {fulltext_stats}", flush=True)

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
        "fulltext": fulltext_stats,
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


# ---------- test-url: test one URL entered in the app (phase 4) ----------

_REQUEST_ID = re.compile(r"^[a-f0-9]{16}$")


def valid_feed_url(url: str) -> bool:
    if not url or len(url) > 500 or any(ch.isspace() for ch in url):
        return False
    p = urlparse(url)
    return p.scheme == "https" and bool(p.hostname) and "@" not in p.netloc


def cmd_test_url(args) -> int:
    url, kind, rid = args.url.strip(), args.type.strip() or "rss", args.request_id.strip()
    result = {"request_id": rid, "url": url, "type": kind, "tested_at": iso(now_utc()), "ok": False}
    if not _REQUEST_ID.match(rid):
        result["error"] = "bad request id"
    elif not valid_feed_url(url):
        result["error"] = "URL ต้องเป็น https และยาวไม่เกิน 500 ตัวอักษร"
    elif kind not in PARSERS:
        result["error"] = f"ไม่รู้จักชนิด {kind}"
    else:
        source = {"id": "test", "name": "test", "url": url, "type": kind, "enabled": False}
        res = check_source(source)
        result.update({k: res.get(k) for k in ("status", "items", "with_summary", "with_content",
                                                "with_image", "age_hours", "error", "notes")})
        result["ok"] = res["status"] in ("OK", "WARN")
        if result["ok"]:
            try:
                items = fetch_source(source)
                result["samples"] = [i["title"] for i in items[:3]]
                if kind == "rss":
                    meta = feedparser.parse(http_get(url)).feed
                    result["feed_title"] = clean_text(meta.get("title"))[:100]
                    result["feed_lang"] = (meta.get("language") or "")[:2].lower()
            except Exception:  # noqa: BLE001 - extras only
                pass
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
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
    t = sub.add_parser("test-url", help="test one feed URL (used by the test-source workflow)")
    t.add_argument("--url", required=True)
    t.add_argument("--type", default="rss")
    t.add_argument("--request-id", required=True)
    t.add_argument("--out", default="result.json")
    t.set_defaults(func=cmd_test_url)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
