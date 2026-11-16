#!/usr/bin/env python3
"""crawler_cli.py - the approved site-crawler CLI for the chat backend.

Contract:
  * stdout: exactly ONE JSON object (always, even on failure)
  * stderr: human-readable progress logs
  * exit codes:
      0 success          - every attempted page was crawled
      1 internal error   - unexpected bug (still emits JSON)
      2 invalid input    - bad flags, bad URL, blocked/non-public host
      3 unreachable      - start page could not be fetched (DNS, timeout, HTTP 4xx/5xx, not HTML)
      4 robots blocked   - robots.txt disallows the start URL
      5 partial          - some pages crawled, but some failed or the time budget ran out

Example:
  python cli/crawler_cli.py --url https://example.com --max-pages 5 --max-depth 1
"""

import argparse
import hashlib
import ipaddress
import json
import re
import socket
import sys
import time
from collections import deque, namedtuple
from datetime import datetime, timezone
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

TOOL_NAME = "crawler_cli"
TOOL_VERSION = "1.0"

EXIT_OK, EXIT_INTERNAL, EXIT_INVALID, EXIT_UNREACHABLE, EXIT_ROBOTS, EXIT_PARTIAL = 0, 1, 2, 3, 4, 5

# Hard caps: values above these are clamped (and reported), values below the minimum are rejected.
DEFAULTS = {"max_pages": 5, "max_depth": 1, "timeout": 10.0, "total_timeout": 45.0}
CAPS = {"max_pages": 20, "max_depth": 3, "timeout": 15.0, "total_timeout": 50.0}
MINS = {"max_pages": 1, "max_depth": 0, "timeout": 1.0, "total_timeout": 1.0}

USER_AGENT = "BuildSprintCrawler/1.0 (educational hackathon project)"
MAX_BYTES = 2_000_000          # per page
MAX_ROBOTS_BYTES = 500_000
MAX_TEXT_CHARS = 5000          # visible text kept per page
MAX_HEADINGS = 30
MAX_LINKS_REPORTED = 25
MAX_REDIRECTS = 5
POLITE_DELAY = 0.3             # seconds between requests
SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
    ".zip", ".gz", ".tar", ".rar", ".mp3", ".mp4", ".avi", ".mov", ".xml", ".json",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".exe", ".dmg",
)

Fetched = namedtuple("Fetched", "url status content_type body truncated")


class InputError(Exception):
    """Bad or disallowed input -> exit 2."""


class FetchError(Exception):
    """Network-level failure (DNS, timeout, connection, too many redirects)."""


class RobotsBlocked(Exception):
    """robots.txt disallows the start URL -> exit 4."""


def log(msg):
    print(f"[crawler] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- URL safety

def validate_url(url):
    """Syntactic checks only. Returns the parsed URL or raises InputError."""
    if not url or len(url) > 2048:
        raise InputError("URL is missing or longer than 2048 characters")
    try:
        p = urlparse(url)
        p.port  # raises ValueError on a bad port
    except ValueError as e:
        raise InputError(f"malformed URL: {e}")
    if p.scheme not in ("http", "https"):
        raise InputError(f"scheme '{p.scheme or '(none)'}' is not allowed; use http or https")
    if not p.hostname:
        raise InputError("URL has no hostname")
    if p.username or p.password:
        raise InputError("credentials inside the URL are not allowed")
    return p


def check_host(hostname):
    """SSRF guard: every address the host resolves to must be public."""
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise FetchError(f"DNS resolution failed for '{hostname}'")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise InputError(f"host '{hostname}' resolves to non-public address {ip}; blocked")


def normalize(url):
    """Strip fragment, lowercase scheme/host, ensure a path."""
    url, _ = urldefrag(url)
    p = urlparse(url)
    return p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower(), path=p.path or "/").geturl()


# ---------------------------------------------------------------- fetching

def fetch(session, url, timeout, deadline=None, max_bytes=MAX_BYTES):
    """GET with manual redirects so every hop passes the SSRF checks. Body is size-capped."""
    for _ in range(MAX_REDIRECTS + 1):
        p = validate_url(url)
        check_host(p.hostname)
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=False, stream=True)
            try:
                if resp.is_redirect:
                    url = normalize(urljoin(url, resp.headers["location"]))
                    log(f"redirect -> {url}")
                    continue
                chunks, size, truncated = [], 0, False
                for chunk in resp.iter_content(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= max_bytes:
                        truncated = True
                        break
                    if deadline and time.monotonic() > deadline:
                        raise FetchError("total time budget exceeded while downloading")
                body = b"".join(chunks)[:max_bytes]
                return Fetched(url, resp.status_code, resp.headers.get("Content-Type", ""), body, truncated)
            finally:
                resp.close()
        except requests.Timeout:
            raise FetchError(f"timed out after {timeout:.0f}s")
        except requests.RequestException as e:
            raise FetchError(f"request failed ({type(e).__name__})")
    raise FetchError(f"more than {MAX_REDIRECTS} redirects")


class RobotsCache:
    """One robots.txt parser per scheme+host, loaded lazily."""

    def __init__(self, session, timeout):
        self.session, self.timeout = session, timeout
        self.parsers, self.status = {}, {}

    def allowed(self, url):
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self.parsers:
            self.parsers[base] = self._load(base)
        rp = self.parsers[base]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def _load(self, base):
        try:
            r = fetch(self.session, base + "/robots.txt", self.timeout, max_bytes=MAX_ROBOTS_BYTES)
        except (FetchError, InputError) as e:
            self.status[base] = "unavailable"
            log(f"robots.txt for {base} unavailable ({e}); proceeding")
            return None
        rp = robotparser.RobotFileParser()
        if r.status in (401, 403):
            self.status[base] = f"access_denied_{r.status}"
            rp.disallow_all = True
            log(f"robots.txt for {base} returned {r.status}; treating as disallow-all")
            return rp
        if r.status >= 400:
            self.status[base] = "not_found"
            log(f"robots.txt for {base}: HTTP {r.status}, no restrictions")
            return None
        rp.parse(r.body.decode("utf-8", errors="replace").splitlines())
        rp.modified()  # can_fetch() returns False if the parser was never marked as read
        self.status[base] = "found"
        log(f"robots.txt for {base}: loaded")
        return rp


# ---------------------------------------------------------------- extraction

def extract_page(fetched, depth):
    """Parse HTML into a structured page record plus the list of discovered links."""
    soup = BeautifulSoup(fetched.body, "html.parser")
    title = " ".join(soup.title.get_text(" ").split()) if soup.title else ""
    desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    description = (desc_tag.get("content") or "").strip() if desc_tag else ""

    links, link_texts = [], {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
            continue
        absolute = urljoin(fetched.url, href)
        if urlparse(absolute).scheme not in ("http", "https"):
            continue
        n = normalize(absolute)
        if urlparse(n).path.lower().endswith(SKIP_EXTENSIONS):
            continue
        links.append(n)
        text = " ".join((a.get("title") or a.get_text(" ")).split())
        if text and n not in link_texts:
            link_texts[n] = text[:120]
    links = list(dict.fromkeys(links))

    for tag in soup(["script", "style", "noscript", "template", "svg", "iframe", "head"]):
        tag.decompose()
    headings = []
    for h in soup.find_all(["h1", "h2", "h3"]):
        t = " ".join(h.get_text(" ").split())
        if t:
            headings.append({"level": h.name, "text": t[:200]})
    text = " ".join(soup.get_text(" ").split())

    page = {
        "url": fetched.url,
        "depth": depth,
        "status_code": fetched.status,
        "title": title[:300],
        "meta_description": description[:500],
        "headings": headings[:MAX_HEADINGS],
        "text": text[:MAX_TEXT_CHARS],
        "text_truncated": len(text) > MAX_TEXT_CHARS or fetched.truncated,
        "word_count": len(text.split()),
        "links": [{"url": u, "text": link_texts.get(u, "")} for u in links[:MAX_LINKS_REPORTED]],
        "links_found": len(links),
    }
    return page, links


# ---------------------------------------------------------------- crawl

def crawl(start_url, limits, result, t0):
    deadline = t0 + limits["total_timeout"]
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    robots = RobotsCache(session, limits["timeout"])

    allowed_hosts = {urlparse(start_url).hostname}
    queue, seen, crawled, content_hashes = deque([(start_url, 0)]), {start_url}, set(), {}
    pages, errors, robots_skipped = result["pages"], result["errors"], []
    budget_exhausted = cancelled = False

    try:
        while queue and len(pages) < limits["max_pages"]:
            if time.monotonic() >= deadline:
                budget_exhausted = True
                log(f"total time budget of {limits['total_timeout']:.0f}s reached; stopping")
                break
            url, depth = queue.popleft()
            is_start = depth == 0

            if not robots.allowed(url):
                if is_start:
                    raise RobotsBlocked(f"robots.txt disallows {url}")
                robots_skipped.append(url)
                log(f"skip (robots.txt) {url}")
                continue

            req_timeout = max(1.0, min(limits["timeout"], deadline - time.monotonic()))
            started = time.monotonic()
            try:
                f = fetch(session, url, req_timeout, deadline=deadline)
            except (FetchError, InputError) as e:
                if is_start:
                    raise
                errors.append({"url": url, "error": str(e)})
                log(f"error {url}: {e}")
                continue
            ms = int((time.monotonic() - started) * 1000)

            if is_start:
                allowed_hosts.add(urlparse(f.url).hostname)  # follow e.g. example.com -> www.example.com
                result["final_start_url"] = f.url
            if f.url in crawled:
                continue
            crawled.add(f.url)
            seen.add(f.url)

            if f.status >= 400:
                if is_start:
                    raise FetchError(f"start page returned HTTP {f.status}")
                errors.append({"url": f.url, "error": f"HTTP {f.status}"})
                log(f"error {f.url}: HTTP {f.status} ({ms}ms)")
                continue
            if "html" not in f.content_type.lower():
                if is_start:
                    raise FetchError(f"start page is not HTML (Content-Type: {f.content_type or 'unknown'})")
                log(f"skip non-HTML {f.url} ({f.content_type})")
                continue

            digest = hashlib.sha256(f.body).hexdigest()
            if digest in content_hashes:  # e.g. "/" and "/index.html" serve the same page
                log(f"skip duplicate content {f.url} (same as {content_hashes[digest]})")
                continue
            content_hashes[digest] = f.url

            page, links = extract_page(f, depth)
            pages.append(page)
            log(f"fetched {f.url} {f.status} ({ms}ms, {page['word_count']} words, {len(links)} links)")

            if depth < limits["max_depth"]:
                for link in links:
                    if urlparse(link).hostname in allowed_hosts and link not in seen:
                        seen.add(link)
                        queue.append((link, depth + 1))

            if queue and len(pages) < limits["max_pages"]:
                time.sleep(POLITE_DELAY)
    except KeyboardInterrupt:
        cancelled = True
        log("cancelled; returning what was collected so far")

    result["robots"] = {"respected": True, "user_agent": USER_AGENT,
                        "hosts": robots.status, "skipped_urls": robots_skipped}
    result["stats"] = {
        "pages_crawled": len(pages),
        "page_errors": len(errors),
        "queued_not_crawled": len(queue),
        "budget_exhausted": budget_exhausted,
        "cancelled": cancelled,
    }
    if not pages:
        result["status"] = "failed"
        result["error"] = {"type": "no_pages", "message": "no HTML pages could be crawled"}
        return EXIT_UNREACHABLE
    if errors or budget_exhausted or cancelled:
        result["status"] = "partial"
        return EXIT_PARTIAL
    result["status"] = "success"
    return EXIT_OK


# ---------------------------------------------------------------- CLI plumbing

def base_result(url):
    return {
        "tool": TOOL_NAME, "version": TOOL_VERSION, "status": "failed", "exit_code": None,
        "start_url": url, "final_start_url": None,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_ms": 0, "limits": {}, "clamped": [],
        "pages": [], "errors": [], "error": None,
    }


def fail(result, code, err_type, message):
    result["status"] = "failed"
    result["error"] = {"type": err_type, "message": message}
    log(f"FAILED ({err_type}): {message}")
    return code


def emit(result, code):
    result["exit_code"] = code
    print(json.dumps(result, indent=2))  # ensure_ascii=True: safe on any console encoding
    sys.stdout.flush()
    return code


def resolve_limits(args):
    limits, clamped = {}, []
    for key in DEFAULTS:
        value = getattr(args, key)
        if value < MINS[key]:
            raise InputError(f"--{key.replace('_', '-')} must be >= {MINS[key]}")
        if value > CAPS[key]:
            clamped.append(f"{key}: {value} -> {CAPS[key]}")
            log(f"clamping {key} {value} -> {CAPS[key]}")
            value = CAPS[key]
        limits[key] = value
    return limits, clamped


class JsonArgParser(argparse.ArgumentParser):
    """argparse that reports bad arguments as JSON on stdout (exit 2) instead of plain usage text."""

    def error(self, message):
        result = base_result(None)
        sys.exit(emit(result, fail(result, EXIT_INVALID, "invalid_input", message)))


def build_parser():
    p = JsonArgParser(prog="crawler_cli", description="Crawl a public website (same domain, robots.txt honored) and print JSON.")
    p.add_argument("--url", required=True, help="start URL (http/https, public hosts only)")
    p.add_argument("--max-pages", type=int, default=DEFAULTS["max_pages"], help=f"pages to crawl (cap {CAPS['max_pages']})")
    p.add_argument("--max-depth", type=int, default=DEFAULTS["max_depth"], help=f"link depth from start page (cap {CAPS['max_depth']})")
    p.add_argument("--timeout", type=float, default=DEFAULTS["timeout"], help=f"per-request timeout seconds (cap {CAPS['timeout']:.0f})")
    p.add_argument("--total-timeout", type=float, default=DEFAULTS["total_timeout"], help=f"whole-run time budget seconds (cap {CAPS['total_timeout']:.0f})")
    return p


def main(argv=None):
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")
    args = build_parser().parse_args(argv)
    t0 = time.monotonic()
    raw_url = args.url.strip()
    result = base_result(raw_url)
    try:
        limits, result["clamped"] = resolve_limits(args)
        result["limits"] = limits
        validate_url(raw_url)
        start = normalize(raw_url)
        result["start_url"] = start
        check_host(urlparse(start).hostname)
        log(f"start {start} (max_pages={limits['max_pages']}, max_depth={limits['max_depth']}, "
            f"timeout={limits['timeout']:.0f}s, budget={limits['total_timeout']:.0f}s)")
        code = crawl(start, limits, result, t0)
    except InputError as e:
        code = fail(result, EXIT_INVALID, "invalid_input", str(e))
    except FetchError as e:
        code = fail(result, EXIT_UNREACHABLE, "unreachable", str(e))
    except RobotsBlocked as e:
        code = fail(result, EXIT_ROBOTS, "robots_blocked", str(e))
    except Exception as e:  # never crash without JSON
        log(f"internal error: {type(e).__name__}: {e}")
        code = fail(result, EXIT_INTERNAL, "internal_error", type(e).__name__)
    result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    log(f"done status={result['status']} exit={code} pages={len(result['pages'])} in {result['elapsed_ms']}ms")
    return emit(result, code)


if __name__ == "__main__":
    sys.exit(main())
