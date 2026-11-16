"""intent.py - natural-language message -> structured Intent.

The LLM only fills in parameters; it never produces commands. Its output is then checked:
  * the URL must literally appear in the user's message (no invented / swapped URLs)
  * numbers are later clamped by validator.py
If the LLM is unavailable, a deterministic regex parser is used instead (and we say so).
"""

import re

from pydantic import BaseModel, Field

from backend import llm

URL_RE = re.compile(r"""https?://[^\s<>"'`)\]]+""", re.I)
BARE_DOMAIN_RE = re.compile(r"\b(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:/[^\s<>\"'`)\]]*)?", re.I)


class Intent(BaseModel):
    task: str = "unsupported"              # crawl_site | unsupported
    url: str | None = None
    max_pages: int = 5
    max_depth: int = 1
    wants_summary: bool = True
    wants_json: bool = True
    fields_requested: list[str] = Field(default_factory=list)
    explanation: str = ""                  # one line: how the request was understood
    source: str = "fallback"               # llm | fallback
    model: str | None = None
    notes: list[str] = Field(default_factory=list)


SYSTEM = """You convert a user's chat message into parameters for a website-crawling tool.
You do NOT execute anything and you do NOT write commands. Output only the JSON object.

Rules:
- task = "crawl_site" if the user wants information from a website (crawl, scrape, summarise, extract, analyse a URL).
  Otherwise task = "unsupported".
- url: copy the URL exactly as written in the message. If the user wrote a bare domain like example.com, prefix https://.
  If there is no URL, use an empty string. Never invent a URL.
- max_pages: number of pages the user asked for; default 5 if not stated. max_depth: default 1 (use 0 if they only want that single page).
- wants_summary: true unless the user clearly does not want a summary.
- wants_json: true if they mention JSON / structured data / extracted data; default true.
- fields_requested: short names of specific things they want extracted (e.g. "prices", "contact email", "product names"). Empty if none.
- explanation: one short sentence describing what will be done."""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "task": {"type": "STRING", "enum": ["crawl_site", "unsupported"]},
        "url": {"type": "STRING"},
        "max_pages": {"type": "INTEGER"},
        "max_depth": {"type": "INTEGER"},
        "wants_summary": {"type": "BOOLEAN"},
        "wants_json": {"type": "BOOLEAN"},
        "fields_requested": {"type": "ARRAY", "items": {"type": "STRING"}},
        "explanation": {"type": "STRING"},
    },
    "required": ["task", "url", "max_pages", "max_depth", "wants_summary", "wants_json", "fields_requested", "explanation"],
}


def _urls_in(message):
    found = [u.rstrip(".,;:!?") for u in URL_RE.findall(message)]
    if not found:
        found = ["https://" + d.rstrip(".,;:!?") for d in BARE_DOMAIN_RE.findall(message)]
    return found


def _norm(u):
    return u.lower().rstrip("/").removeprefix("https://").removeprefix("http://").removeprefix("www.")


def fallback_intent(message, note=None):
    """Deterministic parser: no AI involved."""
    urls = _urls_in(message)
    low = message.lower()
    m = re.search(r"(\d{1,3})\s*(?:pages?|links?)", low)
    intent = Intent(
        task="crawl_site" if urls else "unsupported",
        url=urls[0] if urls else None,
        max_pages=int(m.group(1)) if m else 5,
        max_depth=0 if re.search(r"\b(only|just) (this|that|the) page\b|single page", low) else 1,
        wants_json=True,
        explanation="Parsed with the rule-based fallback (no AI).",
        source="fallback",
    )
    if note:
        intent.notes.append(note)
    if len(urls) > 1:
        intent.notes.append(f"several URLs found; using the first one ({urls[0]})")
    return intent


def parse_intent(message):
    message = message.strip()[:4000]
    if not llm.is_configured():
        return fallback_intent(message, "AI unavailable (no API key); used rule-based parsing")
    try:
        raw, model = llm.generate(SYSTEM, f"User message:\n<<<\n{message}\n>>>", schema=SCHEMA, temperature=0)
    except llm.LLMError as e:
        return fallback_intent(message, f"AI unavailable ({str(e)[:160]}); used rule-based parsing")

    try:
        intent = Intent(**{k: raw.get(k) for k in SCHEMA["properties"] if raw.get(k) is not None},
                        source="llm", model=model)
    except Exception:
        return fallback_intent(message, "AI returned malformed parameters; used rule-based parsing")

    # Guard: the URL must come from the user's message, not from the model's imagination.
    in_message = _urls_in(message)
    if intent.url:
        if not any(_norm(intent.url) == _norm(u) for u in in_message):
            intent.notes.append("AI-proposed URL was not in your message; using the URL you typed instead")
            intent.url = in_message[0] if in_message else None
    elif in_message and intent.task == "crawl_site":
        intent.url = in_message[0]
    if intent.task == "crawl_site" and not intent.url:
        intent.task = "unsupported"
        intent.notes.append("no website URL found in the message")
    if not intent.url:
        intent.url = None
    return intent
