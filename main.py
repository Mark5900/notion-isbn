"""Notion-ISBN: fill in book data in a Notion database from the ISBN.

Normal mode scans the database every SCAN_INTERVAL seconds for pages whose
title is a placeholder ("New Book", "New Physical Book", "New Digital Book" ...)
and fills them from Google Books and Open Library.

Backfill mode (`python main.py --backfill [--dry-run]`) goes through every page
once and fills only fields that are empty or marked unknown. It never
overwrites data that is already there.
"""

import argparse
import logging
import os
import re
import sys
import textwrap
import time
from io import BytesIO

import requests
import schedule
from bs4 import BeautifulSoup
from PIL import Image, ImageFilter

# ---------------------------------------------------------------- config ----

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
GOOGLE_API_KEY = os.getenv("GoogleAPIKey") or os.getenv("GOOGLE_API_KEY")
GOOGLE_COUNTRY = os.getenv("GOOGLE_COUNTRY", "DK")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
RETRY_NOT_FOUND_HOURS = float(os.getenv("RETRY_NOT_FOUND_HOURS", "6"))
NEW_BOOK_PATTERN = re.compile(os.getenv("NEW_BOOK_PATTERN", r"^\s*New(\s+\w+)?\s+Book\s*$"), re.I)
UNKNOWN_VALUES = {"", "unknown", "unknown author", "unknown publisher", "no publisher found", "anthology", "unkknow"}

USE_PUSHBULLET = os.getenv("USE_PUSHBULLET", "no").lower() == "yes"
USE_AWS = os.getenv("USE_AWS", "no").lower() == "yes"
AWS_BUCKET = os.getenv("AWS_BUCKET")
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
AWS_CLEANUP_FILES = USE_AWS and os.getenv("AWS_CLEANUP_FILES", "no").lower() == "yes"
S3_BASE = f"https://{AWS_BUCKET}.s3.{AWS_REGION}.amazonaws.com"
NO_COVER_URL = f"{S3_BASE}/noCover.jpg" if USE_AWS else None
IMAGE_OVERRIDE_ID = os.getenv("NOTION_IMAGE_OVERRIDE_ID")

NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger("notion-isbn")

http = requests.Session()
http.headers["User-Agent"] = "notion-isbn/2.0 (+https://github.com/allaboutduncan/notion-isbn)"

pb = None
if USE_PUSHBULLET:
    from pushbullet import Pushbullet
    pb = Pushbullet(os.getenv("PB_TOKEN"))


def send_push(subject, message):
    if pb:
        try:
            pb.push_note(subject, message)
        except Exception as e:  # never let a notification kill the scan
            log.warning("Pushbullet failed: %s", e)


# ------------------------------------------------------------------ ISBN ----

def clean_isbn(raw):
    """Return the ISBN as digits (and a trailing X), or None if it is not valid."""
    isbn = re.sub(r"[^0-9Xx]", "", raw or "").upper()
    if len(isbn) == 13 and isbn.isdigit():
        total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(isbn))
        return isbn if total % 10 == 0 else None
    if len(isbn) == 10 and isbn[:9].isdigit():
        total = sum((10 - i) * (10 if d == "X" else int(d)) for i, d in enumerate(isbn))
        return isbn if total % 11 == 0 else None
    return None


def isbn10_to_13(isbn):
    if len(isbn) != 10:
        return isbn
    core = "978" + isbn[:9]
    check = (10 - sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(core)) % 10) % 10
    return core + str(check)


def isbn13_to_10(isbn):
    if len(isbn) != 13 or not isbn.startswith("978"):
        return None
    core = isbn[3:12]
    check = (11 - sum((10 - i) * int(d) for i, d in enumerate(core)) % 11) % 11
    return core + ("X" if check == 10 else str(check))


# --------------------------------------------------------------- sources ----

def _year(value):
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(value or ""))
    return int(m.group(1)) if m else None


def _text(value):
    if isinstance(value, dict):
        value = value.get("value", "")
    text = BeautifulSoup(value or "", "html.parser").get_text(" ")
    text = re.split(r"\n-{3,}|\(\[source\]|\[Source\]", text)[0]
    return re.sub(r"\s+", " ", text).replace("**", "").strip()


def from_google(isbn):
    if not GOOGLE_API_KEY:
        return None
    params = {"q": f"isbn:{isbn}", "key": GOOGLE_API_KEY}
    if GOOGLE_COUNTRY:
        params["country"] = GOOGLE_COUNTRY
    try:
        r = http.get("https://www.googleapis.com/books/v1/volumes", params=params, timeout=20)
        r.raise_for_status()
        items = r.json().get("items") or []
    except requests.RequestException as e:
        # never log e itself: its message contains the URL with the API key
        status = getattr(e.response, "status_code", None)
        log.error("Google Books failed for %s: %s", isbn, status or type(e).__name__)
        return None
    if not items:
        return None
    v = items[0]["volumeInfo"]
    # Google sometimes has placeholder records such as "Untitled Project 1"
    if not (v.get("title") or "").strip() or v["title"].strip().lower().startswith("untitled"):
        return None
    cover = (v.get("imageLinks") or {}).get("thumbnail")
    return {
        "title": v.get("title"),
        "subtitle": v.get("subtitle"),
        "authors": v.get("authors") or [],
        "publisher": v.get("publisher"),
        "year": _year(v.get("publishedDate")),
        "pages": v.get("pageCount") or None,
        "description": _text(v.get("description")),
        "cover": cover.replace("http://", "https://").replace("&edge=curl", "") if cover else None,
    }


def from_openlibrary(isbn):
    key = f"ISBN:{isbn}"
    try:
        data = http.get("https://openlibrary.org/api/books",
                        params={"bibkeys": key, "jscmd": "data", "format": "json"}, timeout=20).json().get(key)
        details = http.get("https://openlibrary.org/api/books",
                           params={"bibkeys": key, "jscmd": "details", "format": "json"}, timeout=20).json().get(key)
    except (requests.RequestException, ValueError) as e:
        log.error("Open Library failed for %s: %s", isbn, e)
        return None
    if not data and not details:
        return None
    data = data or {}
    det = (details or {}).get("details", {})
    description = det.get("description")
    if not description and det.get("works"):
        try:
            work = http.get(f"https://openlibrary.org{det['works'][0]['key']}.json", timeout=20).json()
            description = work.get("description")
        except (requests.RequestException, ValueError):
            pass
    cover = (data.get("cover") or {}).get("large")
    return {
        "title": data.get("title") or det.get("title"),
        "subtitle": data.get("subtitle") or det.get("subtitle"),
        "authors": [a["name"] for a in data.get("authors", [])] or [a["name"] for a in det.get("authors", [])],
        "publisher": (data.get("publishers") or [{}])[0].get("name") or (det.get("publishers") or [None])[0],
        "year": _year(data.get("publish_date") or det.get("publish_date")),
        "pages": data.get("number_of_pages") or det.get("number_of_pages"),
        "description": _text(description),
        "cover": cover,
    }


CITIES = {"london", "new york", "københavn", "kbh", "copenhagen", "stockholm", "oslo", "aarhus", "århus",
           "oxford", "cambridge", "boston", "chicago", "toronto", "sydney", "paris", "berlin", "s.l", "s. l"}


def from_libris(isbn):
    """Libris (Swedish union catalogue, no key). Good for Scandinavian and many UK editions."""
    import xml.etree.ElementTree as ET
    ns = {"m": "http://www.loc.gov/MARC21/slim"}
    try:
        r = http.get("https://libris.kb.se/xsearch",
                     params={"query": f"isbn:{isbn}", "format": "marcxml", "n": 5}, timeout=20)
        r.raise_for_status()
        records = ET.fromstring(r.content).findall(".//m:record", ns)
    except (requests.RequestException, ET.ParseError) as e:
        log.error("Libris failed for %s: %s", isbn, type(e).__name__)
        return None

    def sub(rec, tag, code):
        return [s.text or "" for f in rec.findall(f"m:datafield[@tag='{tag}']", ns)
                for s in f.findall(f"m:subfield[@code='{code}']", ns)]

    def tidy(s):
        return re.sub(r"[\s/:;,.=]+$", "", s or "").strip()

    for rec in records:
        # the search also matches ISBNs elsewhere in the record; keep only exact edition matches
        if not any(clean_isbn(re.split(r"\s", v)[0]) == isbn for v in sub(rec, "020", "a")):
            continue
        authors = []
        for f in rec.findall("m:datafield[@tag='100']", ns) + rec.findall("m:datafield[@tag='700']", ns):
            roles = [s.text for s in f.findall("m:subfield[@code='4']", ns)]
            if f.get("tag") == "700" and roles and "aut" not in roles:
                continue  # skip translators, illustrators ...
            name = (sub_a.text if (sub_a := f.find("m:subfield[@code='a']", ns)) is not None else "") or ""
            # keep the period of an initial ("Tolkien, J. R. R.") but drop the cataloguer's trailing comma/period
            name = re.sub(r"(?<=[a-zæøåäöü]{2})\.$", "", re.sub(r"[\s,:;/]+$", "", name))
            if "," in name:
                last, first = name.split(",", 1)
                name = f"{first.strip()} {last.strip()}"
            if name:
                authors.append(name)
        # "264 s." / "xii, 352 pages" -> last number that is followed by a page unit ("1 volume" is ignored)
        pages = next((int(m[-1]) for v in sub(rec, "300", "a")
                      for m in [re.findall(r"(\d+)\s*(?:s\b|sider|sidor|p\b|pp|pages)", v)] if m), None)
        place = tidy((sub(rec, "264", "a") or sub(rec, "260", "a") or [""])[0])
        publisher = tidy((sub(rec, "264", "b") or sub(rec, "260", "b") or [""])[0])
        if publisher.lower() in CITIES and place.lower() not in CITIES:
            publisher = place  # place and publisher swapped in the record
        publisher = re.sub(r"\bforl\b\.?", "Forlag", publisher)
        return {
            "title": tidy((sub(rec, "245", "a") or [""])[0]),
            "subtitle": tidy((sub(rec, "245", "b") or [""])[0]),
            "authors": authors,
            "publisher": publisher if publisher.lower() not in CITIES else "",
            "year": _year((sub(rec, "264", "c") or sub(rec, "260", "c") or [""])[0]),
            "pages": pages,
            "description": _text((sub(rec, "520", "a") or [""])[0]),
            "cover": None,
        }
    return None


def looks_foreign(text):
    """Open Library sometimes returns a description in a third language."""
    return bool(re.search(r"[ąęłśżźćńřěšůďťňő]", text or ""))


def get_book(raw_isbn):
    isbn = clean_isbn(raw_isbn)
    if not isbn:
        log.warning("ISBN %r is not valid (wrong length or check digit)", raw_isbn)
        return None, None
    candidates = [isbn10_to_13(isbn)]
    alt = isbn if len(isbn) == 10 else isbn13_to_10(isbn)
    if alt:
        candidates.append(alt)

    results = []
    for source in (from_google, from_openlibrary, from_libris):
        for c in candidates:
            res = source(c)
            if res:
                results.append(res)
                break
    if not results:
        return isbn, None

    merged = {}
    for field in ("title", "subtitle", "authors", "publisher", "year", "pages", "description", "cover"):
        for res in results:
            value = res.get(field)
            if field == "description" and (looks_foreign(value) or len(value or "") < 40):
                continue
            if value:
                merged[field] = value
                break
    if not merged.get("cover"):
        merged["cover"] = openlibrary_cover(isbn10_to_13(isbn)) or isbndb_cover(isbn10_to_13(isbn))
    return isbn, merged


def isbndb_cover(isbn13):
    """Last-resort cover: scrape the cover image from the ISBNdb book page."""
    try:
        r = http.get(f"https://isbndb.com/book/{isbn13}", timeout=20,
                     headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        obj = BeautifulSoup(r.content, "html.parser").select_one("div.artwork object[data]")
        return obj["data"] if obj else None
    except requests.RequestException:
        return None


def openlibrary_cover(isbn13):
    url = f"https://covers.openlibrary.org/b/isbn/{isbn13}-L.jpg?default=false"
    try:
        r = http.head(url, timeout=15, allow_redirects=True)
        return url.replace("?default=false", "") if r.status_code == 200 else None
    except requests.RequestException:
        return None


# ---------------------------------------------------------------- notion ----

def notion(method, path, **kwargs):
    for attempt in range(5):
        r = http.request(method, f"{NOTION_API}{path}", headers=NOTION_HEADERS, timeout=30, **kwargs)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
            continue
        return r
    return r


SCHEMA = {}
OPTIONS = {}  # property -> {lowercase option name: existing option name}


def option(prop, name):
    """Reuse an existing select option that differs only in case ("Politikens forlag" -> "Politikens Forlag")."""
    name = select_value(name)
    return OPTIONS.get(prop, {}).get(name.lower(), name)


def load_schema():
    r = notion("GET", f"/databases/{DATABASE_ID}")
    r.raise_for_status()
    for name, prop in r.json()["properties"].items():
        SCHEMA[name] = prop["type"]
        if prop["type"] in ("select", "multi_select"):
            OPTIONS[name] = {o["name"].lower(): o["name"] for o in prop[prop["type"]]["options"]}
    log.info("Database schema loaded (%d properties)", len(SCHEMA))


def query_pages(filter_=None):
    payload = {"page_size": 100}
    if filter_:
        payload["filter"] = filter_
    while True:
        r = notion("POST", f"/databases/{DATABASE_ID}/query", json=payload)
        r.raise_for_status()
        data = r.json()
        yield from data["results"]
        if not data.get("has_more"):
            return
        payload["start_cursor"] = data["next_cursor"]


def prop_text(props, name):
    p = props.get(name) or {}
    kind = p.get("type")
    if kind in ("title", "rich_text"):
        return "".join(t.get("plain_text", "") for t in p[kind])
    if kind == "select":
        return (p["select"] or {}).get("name", "")
    if kind == "multi_select":
        return ", ".join(o["name"] for o in p["multi_select"])
    if kind == "number":
        return p["number"]
    if kind == "files":
        return p["files"]
    return None


def is_empty(value):
    if value in (None, 0, [], ""):
        return True
    return isinstance(value, str) and value.strip().lower() in UNKNOWN_VALUES


def select_value(name):
    # Notion select options may not contain commas and are capped at 100 chars
    return re.sub(r"\s+", " ", name.replace(",", "")).strip()[:100]


def build_title(book):
    title = (book.get("title") or "").strip()
    subtitle = (book.get("subtitle") or "").strip()
    if subtitle and subtitle.lower() not in title.lower():
        title = f"{title.rstrip(': ')}: {subtitle}"
    # drop series notes like "(The Grisha, #1)"
    title = re.sub(r"\s*\([^)]*\)", "", title).strip() or title
    if len(title) > 200:
        title = textwrap.shorten(title, width=200, placeholder="…")
    return title


def build_properties(book, isbn, current=None):
    """Return Notion properties to set. With `current`, only fill empty fields."""
    current = current or {}
    props = {}

    def want(name):
        return name in SCHEMA and (not current or is_empty(prop_text(current, name)))

    title_prop = next((n for n, t in SCHEMA.items() if t == "title"), "Name")
    if book.get("title") and (not current or NEW_BOOK_PATTERN.match(prop_text(current, title_prop) or "")):
        props[title_prop] = {"title": [{"text": {"content": build_title(book)}}]}

    if book.get("authors") and want("Author"):
        names = [option("Author", a) for a in book["authors"] if a]
        if SCHEMA["Author"] == "multi_select":
            props["Author"] = {"multi_select": [{"name": n} for n in dict.fromkeys(names)]}
        elif SCHEMA["Author"] == "select":
            props["Author"] = {"select": {"name": select_value(" & ".join(names))}}
        else:
            props["Author"] = {"rich_text": [{"text": {"content": ", ".join(names)}}]}

    if book.get("publisher"):
        # "Akademisk Forlag Eksp.: DBK" -> "Akademisk Forlag" (drop distributor notes)
        book["publisher"] = re.split(r"\s*(?:Eksp\.|[;:]\s*distribut|\bdistributed by\b)", book["publisher"],
                                     flags=re.I)[0].strip() or book["publisher"]
    if book.get("publisher") and want("Publisher"):
        if SCHEMA["Publisher"] == "select":
            props["Publisher"] = {"select": {"name": option("Publisher", book["publisher"])}}
        else:
            props["Publisher"] = {"rich_text": [{"text": {"content": book["publisher"]}}]}

    if book.get("year") and want("Year"):
        props["Year"] = {"number": book["year"]}
    if book.get("pages") and want("Pages"):
        props["Pages"] = {"number": int(book["pages"])}
    if book.get("description") and want("Summary"):
        props["Summary"] = {"rich_text": [{"text": {"content": textwrap.shorten(
            book["description"], width=2000, placeholder="…")}}]}
    if "ISBN" in SCHEMA and not current:
        props["ISBN"] = {"rich_text": [{"text": {"content": isbn}}]}
    return props


def update_page(page_id, payload):
    r = notion("PATCH", f"/pages/{page_id}", json=payload)
    if r.status_code == 200:
        return True
    body = r.json()
    log.error("Notion update of %s failed (%s): %s", page_id, r.status_code, body.get("message"))
    send_push(f"Notion error {r.status_code}", body.get("message", ""))
    return False


# ----------------------------------------------------------------- cover ----

def make_banner(img_url, page_id):
    """Upload cover + blurred banner to S3 and return (cover_url, banner_url)."""
    import boto3
    from botocore.exceptions import ClientError

    raw = http.get(img_url, timeout=30).content
    img = Image.open(BytesIO(raw)).convert("RGB")
    if img.width < 50 or img.height < 50:
        return None, None
    s3 = boto3.client("s3", region_name=AWS_REGION)
    poster = img.resize((int(540 * img.width / img.height), 540))
    banner = (img.crop((5, img.height // 3, img.width, 2 * img.height // 3))
              .resize((1500, 600)).filter(ImageFilter.BoxBlur(30)))
    banner.paste(poster, ((1500 - poster.width) // 2, 30))
    base = S3_BASE
    try:
        for folder, image in (("book_covers", img), ("book_banners", banner)):
            buf = BytesIO()
            image.save(buf, "JPEG")
            buf.seek(0)
            s3.upload_fileobj(buf, AWS_BUCKET, f"{folder}/{page_id}.jpg", ExtraArgs={"ContentType": "image/jpeg"})
    except ClientError as e:
        log.error("S3 upload failed: %s", e)
        return None, None
    return f"{base}/book_covers/{page_id}.jpg", f"{base}/book_banners/{page_id}.jpg"


def cover_payload(book, page_id, title, use_placeholder=False):
    url = book.get("cover")
    if not url:
        if not (use_placeholder and NO_COVER_URL):
            return {}
        cover = banner = NO_COVER_URL
    else:
        url = url.replace("http://", "https://")
        cover, banner = url, url
        if USE_AWS:
            try:
                c, b = make_banner(url, page_id)
                if c:
                    cover, banner = c, b
                elif use_placeholder:  # image too small to be a real cover
                    cover = banner = NO_COVER_URL
            except Exception as e:
                log.warning("Could not build banner for %s: %s", page_id, e)
    payload = {"cover": {"external": {"url": banner}}}
    if "Cover" in SCHEMA:
        payload.setdefault("properties", {})["Cover"] = {
            "files": [{"name": (title or "cover")[:100], "external": {"url": cover}}]}
    return payload


def ensure_nocover_exists():
    """Upload noCover.jpg to the bucket root once, so all pages can share it."""
    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.client("s3", region_name=AWS_REGION)
    try:
        s3.head_object(Bucket=AWS_BUCKET, Key="noCover.jpg")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchKey"):
            log.error("Could not check noCover.jpg in S3: %s", e)
            return
        try:
            s3.upload_file("noCover.jpg", AWS_BUCKET, "noCover.jpg", ExtraArgs={"ContentType": "image/jpeg"})
            log.info("Uploaded noCover.jpg to %s", NO_COVER_URL)
        except Exception as e:
            log.error("Failed to upload noCover.jpg: %s", e)


def cleanup_unused_s3_images():
    """Delete covers/banners in S3 that no Notion page points to any more."""
    import boto3
    s3 = boto3.client("s3", region_name=AWS_REGION)
    try:
        in_s3 = set()
        for prefix in ("book_covers/", "book_banners/"):
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=AWS_BUCKET, Prefix=prefix):
                in_s3.update(o["Key"] for o in page.get("Contents", []))

        used = set()
        pages = list(query_pages())
        for page in pages:
            urls = [((page.get("cover") or {}).get("external") or {}).get("url", "")]
            urls += [(f.get("external") or {}).get("url", "")
                     for f in (page["properties"].get("Cover") or {}).get("files", [])]
            used.update(u.split(f"{S3_BASE}/", 1)[1] for u in urls if u.startswith(f"{S3_BASE}/"))

        # Safety: an empty or failed Notion read must never wipe the bucket
        if not pages or (in_s3 and not used):
            log.warning("S3 cleanup skipped: no pages or no S3 references found in Notion")
            return
        unused = in_s3 - used
        for key in unused:
            s3.delete_object(Bucket=AWS_BUCKET, Key=key)
        log.info("S3 cleanup: %d images in S3, %d in use, %d deleted", len(in_s3), len(used), len(unused))
    except Exception as e:
        log.error("S3 cleanup failed: %s", e)
        send_push("AWS S3 cleanup error", str(e))


# --------------------------------------------------------- image override ----

def process_image_overrides():
    """Apply custom cover images from the override database (ISBN + Link, Status checkbox)."""
    payload = {"page_size": 100, "filter": {"property": "Status", "checkbox": {"equals": False}}}
    entries = []
    while True:
        r = notion("POST", f"/databases/{IMAGE_OVERRIDE_ID}/query", json=payload)
        if r.status_code != 200:
            log.warning("Image override database query failed (%s): %s", r.status_code, r.json().get("message"))
            return
        data = r.json()
        entries += data["results"]
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    for entry in entries:
        props = entry["properties"]
        isbn = clean_isbn(prop_text(props, "ISBN")) or (prop_text(props, "ISBN") or "").strip()
        link = None
        for name in ("Link", "Name"):
            p = props.get(name) or {}
            if p.get("type") == "url":
                link = p.get("url")
            elif p.get("type") in ("title", "rich_text"):
                text = (prop_text(props, name) or "").strip()
                link = next((t.get("href") for t in p[p["type"]] if t.get("href")), None) or (
                    text if text.startswith("http") else None)
            if link:
                break
        if not isbn or not link:
            log.warning("Image override %s is missing ISBN or Link", entry["id"])
            continue

        books = list(query_pages({"property": "ISBN", "rich_text": {"equals": isbn}}))
        if not books:
            log.error("Image override: no book with ISBN %s", isbn)
            send_push("Image override error", f"Book with ISBN {isbn} not found")
            continue
        book = books[0]
        title = prop_text(book["properties"], "Name") or "cover"
        try:
            cover, banner = make_banner(link, book["id"])
        except Exception as e:
            cover = None
            log.error("Image override for %s failed: %s", isbn, e)
        if not cover:
            send_push("Image override error", f"Could not use the image for ISBN {isbn}")
            continue
        if update_page(book["id"], {"cover": {"external": {"url": banner}}, "properties": {
                "Cover": {"files": [{"name": title[:100], "external": {"url": cover}}]}}}):
            notion("PATCH", f"/pages/{entry['id']}", json={"properties": {"Status": {"checkbox": True}}})
            log.info("Applied custom cover to '%s'", title)
            send_push("Custom book cover applied", f"Updated '{title}' (ISBN {isbn})")


# ------------------------------------------------------------------ runs ----

_not_found = {}  # isbn -> time of last failed lookup


def process_new_books():
    title_prop = next((n for n, t in SCHEMA.items() if t == "title"), "Name")
    pages = query_pages({"property": title_prop, "title": {"starts_with": "New"}})
    for page in pages:
        props = page["properties"]
        title = prop_text(props, title_prop) or ""
        if not NEW_BOOK_PATTERN.match(title):
            continue
        raw_isbn = prop_text(props, "ISBN")
        if not raw_isbn:
            continue
        if time.time() - _not_found.get(raw_isbn, 0) < RETRY_NOT_FOUND_HOURS * 3600:
            continue

        isbn, book = get_book(raw_isbn)
        if not book:
            _not_found[raw_isbn] = time.time()
            log.warning("No data found for ISBN %s (will retry in %sh)", raw_isbn, RETRY_NOT_FOUND_HOURS)
            send_push(f"No data found for ISBN {raw_isbn}", "Check the ISBN, or fill the page in by hand.")
            continue

        payload = {"properties": build_properties(book, isbn)}
        new_title = build_title(book)
        cov = cover_payload(book, page["id"], new_title, use_placeholder=True)
        payload["properties"].update(cov.pop("properties", {}))
        payload.update(cov)
        if update_page(page["id"], payload):
            log.info("Added %s (%s)", new_title, isbn)
            send_push("New book found!", f"Added {new_title} to your book collection")


def backfill(dry_run=False):
    """Fill empty/unknown fields on every page that has an ISBN."""
    changed = 0
    for page in query_pages():
        props = page["properties"]
        raw_isbn = prop_text(props, "ISBN")
        if not raw_isbn:
            continue
        missing = [n for n in ("Author", "Publisher", "Year", "Pages", "Summary", "Cover")
                   if n in SCHEMA and is_empty(prop_text(props, n))]
        if not missing:
            continue
        isbn, book = get_book(raw_isbn)
        if not book:
            log.info("No data for %s (%s)", raw_isbn, prop_text(props, "Name"))
            continue
        payload = {"properties": build_properties(book, isbn, current=props)}
        if "Cover" in missing and dry_run and book.get("cover"):
            payload["cover"] = book["cover"]  # only for the log; no S3 upload in a dry run
        elif "Cover" in missing:
            cov = cover_payload(book, page["id"], prop_text(props, "Name"))
            payload["properties"].update(cov.pop("properties", {}))
            if not page.get("cover"):
                payload.update(cov)
        if not payload["properties"] and "cover" not in payload:
            continue
        changed += 1
        log.info("%s %s: %s", "Would update" if dry_run else "Updating",
                 prop_text(props, "Name"), ", ".join(payload["properties"]) or "cover")
        if dry_run:
            for name, value in payload["properties"].items():
                log.info("    %s = %s", name, textwrap.shorten(str(value), 160))
            if "cover" in payload:
                log.info("    cover = %s", payload["cover"])
        if not dry_run:
            update_page(page["id"], payload)
        time.sleep(0.4)  # stay under Notion's ~3 requests/second
    log.info("Backfill done: %d pages %s", changed, "would change" if dry_run else "changed")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backfill", action="store_true", help="fill empty fields on all existing books, then exit")
    parser.add_argument("--dry-run", action="store_true", help="with --backfill: only log what would change")
    parser.add_argument("--once", action="store_true", help="scan for new books once and exit")
    args = parser.parse_args()

    if not NOTION_TOKEN or not DATABASE_ID:
        sys.exit("NOTION_TOKEN and NOTION_DATABASE_ID must be set")
    if not GOOGLE_API_KEY:
        log.warning("No Google Books API key set; only Open Library is used (poor coverage for Danish books)")
    load_schema()

    if args.backfill:
        backfill(dry_run=args.dry_run)
        return
    def scan():
        process_new_books()
        if USE_AWS and IMAGE_OVERRIDE_ID:
            process_image_overrides()

    if USE_AWS:
        ensure_nocover_exists()
    scan()
    if args.once:
        return
    schedule.every(SCAN_INTERVAL).seconds.do(scan)
    if AWS_CLEANUP_FILES:
        cleanup_unused_s3_images()
        schedule.every().day.do(cleanup_unused_s3_images)
    log.info("Scanning every %s seconds...", SCAN_INTERVAL)
    while True:
        try:
            schedule.run_pending()
        except Exception as e:  # keep the container alive on transient errors
            log.exception("Scan failed: %s", e)
        time.sleep(1)


if __name__ == "__main__":
    main()
