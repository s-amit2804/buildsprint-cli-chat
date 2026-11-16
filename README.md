# crawl-chat

A chat interface where a plain-English request such as

> *"Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format."*

makes the backend **start a real CLI process** (an allowlisted crawler) and capture its **stdout, stderr, exit code and PID**. The backend validates that output and returns an **honest status**, a **summary** and **valid JSON** to the chat. Every extracted fact carries a quote from the page, and the server **grades each quote against the crawled text** before showing it.

Built for BuildSprint Round 2. Runs locally; there is no deployment.

![A real run: books.toscrape.com, 3 pages](docs/img/books-run.png)

*A real run, not a mock-up. It shows the timed steps, the exact argv (`shell=False`), the crawler's live stderr with timestamps, PID, stdout size and hash, the audit file, and 10/10 facts verified against the crawl. The JSON from this run is in [docs/sample-output.json](docs/sample-output.json).*

---

## Quick start

Prerequisites: **Python 3.10+** and a Gemini API key. The key is optional: without it the app uses rule-based parsing and summaries, and labels the results that way.

**Windows (PowerShell)**
```powershell
git clone https://github.com/s-amit2804/buildsprint-cli-chat.git
cd buildsprint-cli-chat
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env        # then edit .env and set GEMINI_API_KEY
uvicorn backend.main:app --port 8000
```

**macOS / Linux**
```bash
git clone https://github.com/s-amit2804/buildsprint-cli-chat.git
cd buildsprint-cli-chat
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit .env and set GEMINI_API_KEY
uvicorn backend.main:app --port 8000
```

Open **http://127.0.0.1:8000**, then type `1` and press Enter to run the mandatory demo, or type your own request. `help` lists the examples, `clear` clears the screen, and `Esc` cancels a running crawl.

### Environment variables (`.env`, git-ignored)
| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | recommended | Gemini key for intent parsing and summaries. If it's missing, the rule-based fallbacks are used. |
| `GEMINI_MODEL` | no | Try this model first. The app falls back through `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite` → `gemini-3-flash-preview`. |
| `ALLOW_TESTPAGES` | no | `1` enables the local prompt-injection demo pages. Off by default. |
| `APP_PORT` | no | Port used to build the test-page URL (default `8000`). It must match the port uvicorn uses. |

---

## How to test

| Type in the chat | Expected result |
|---|---|
| `1`: *Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format.* | **SUCCESS**: summary, graded facts, JSON |
| `2`: *Crawl 3 pages of https://books.toscrape.com and return book titles and prices as JSON* | **SUCCESS**: titles and prices, each graded |
| `3`: *Summarise http://169.254.169.254/latest/meta-data* | **REJECTED** before any process starts (SSRF guard) |
| `4`: *Crawl https://www.google.com/search?q=test* | **FAILED**: the CLI ran and exited with code 4 (robots.txt) |
| `5`: *Crawl https://this-domain-does-not-exist-xyz.com* | **FAILED**: exit code 3 (unreachable) |
| *hello, how are you?* | **NOT RUN**: asks for a URL |
| A 15-page crawl, then `Esc` | **CANCELLED**: the process is stopped on the server |

![Edge cases: SSRF rejection, robots.txt failure, prompt-injection page](docs/img/edge-cases.png)

**Prompt-injection demo.** Start the server with `ALLOW_TESTPAGES=1`:
- PowerShell: `$env:ALLOW_TESTPAGES="1"; uvicorn backend.main:app --port 8000`
- bash: `ALLOW_TESTPAGES=1 uvicorn backend.main:app --port 8000`

Then type `6`. [testpages/menu.html](testpages/menu.html) hides a `display:none` block telling the AI to declare the site safe, reveal its prompt and API keys, and recommend `curl … | sh`. What happens:
- The **crawler** flags it (`hidden_instructions`) before any AI sees it.
- The chat shows `[ WARN ]`.
- The summary treats it as data and mentions the attempt.

Only the exact `/testpages/` prefix is allowed; anything else on localhost is still blocked.

**Run the CLI on its own:**
```powershell
python cli/crawler_cli.py --url https://example.com; echo "exit=$LASTEXITCODE"
python cli/crawler_cli.py --url https://books.toscrape.com --max-pages 4
python cli/crawler_cli.py --url https://example.com 2>$null | python -m json.tool   # stdout is pure JSON
```

**Run the runner on its own** (validation, subprocess, capture, status):
```powershell
python -m backend.runner --url https://books.toscrape.com --max-pages 3
python -m backend.runner --url https://books.toscrape.com --max-pages 10 --timeout 3   # forces TIMEOUT
```

**Tests:** `python -m tests.test_offline` runs 34 checks with no API key and no internet access beyond DNS. They include a **local HTTP server** that serves:
- an open redirect to `169.254.169.254`
- a redirect to another local port
- a redirect loop
- a 404 sub-page
- a 500 KB JSON body
- a hidden injection block

The tests assert the exact exit code and error for each. They also cover the connect-time guard and evidence grading (including "real quote, wrong value").

Every request, including rejected ones, leaves one audit file at `runs/<run_id>.json` with the full event trace, timings, argv, exit code, stderr and the parsed output. That folder is git-ignored.

---

## Architecture

```mermaid
flowchart LR
  U[Chat UI<br/>static/index.html] -- POST /api/chat/stream --> M[FastAPI<br/>backend/main.py]
  M --> O[Orchestrator<br/>orchestrator.py]
  O --> I[Intent<br/>intent.py → Gemini JSON mode<br/>or regex fallback]
  O --> V[Validator<br/>validator.py · Pydantic + SSRF]
  O --> R[Runner<br/>runner.py · Popen shell=False<br/>timeout · cancel · capture]
  R -- argv list --> C[[CLI: cli/crawler_cli.py<br/>requests + BeautifulSoup]]
  C -- stdout JSON · stderr log · exit code --> R
  O --> S[Summarizer<br/>summarizer.py → Gemini<br/>+ evidence grading]
  M -- SSE: step · plan · command · log · security · final --> U
```

| Step | Component | What happens |
|---|---|---|
| 1 | **Chat UI** | Events stream back over Server-Sent Events, so steps, the CLI log and warnings appear as they happen. All page-derived text is inserted with `textContent`, never `innerHTML`. |
| 2 | **Intent** ([intent.py](backend/intent.py)) | Gemini, in JSON mode with a fixed schema, fills `task, url, max_pages, max_depth, wants_json, fields_requested`. **The URL must literally appear in the user's message**, so the model can't invent or swap targets. If Gemini fails, a regex parser takes over and the result says so. |
| 3 | **Validate** ([validator.py](backend/validator.py)) | A Pydantic model rejects unknown fields. Only http/https, no credentials, and the host must resolve **only to public IPs**. Limits are clamped (≤20 pages, depth ≤3). |
| 4 | **Run CLI** ([runner.py](backend/runner.py)) | The tool comes from an **allowlist**. The argv is built only from validated fields and started with `Popen([...], shell=False)` in a minimal environment **without API keys**. Reader threads stream stderr live and capture stdout (capped at 8 MB). A watchdog enforces the 60s hard timeout and user cancel. PID, stdout size and SHA-256 are recorded. |
| 5 | **Check output** | stdout must be a JSON object, and its `exit_code` must equal the real one. "Success" with zero pages counts as failure. Status is one of `success / partial / failed / timeout / cancelled`. |
| 6 | **Summarise** ([summarizer.py](backend/summarizer.py)) | Page text goes inside `<untrusted_page_content>` tags, with spoofed tags stripped. The model has **no tools** and returns facts as `{field, value, source_url, evidence}`. |
| 7 | **Grade evidence** | **verified** means the quote is on the page verbatim *and* contains the value. **paraphrased** means the value is on the page but the quote isn't exact. **unverified** covers everything else. The value is checked as well as the quote, because a model can quote a real sentence next to a wrong number. |
| 8 | **Respond** ([orchestrator.py](backend/orchestrator.py)) | The server builds the final JSON (`schema_version` 1.1) from the crawl and the graded facts, and writes the audit file. |

---

## The CLI tool: `cli/crawler_cli.py`

```
python cli/crawler_cli.py --url URL [--max-pages 5] [--max-depth 1] [--timeout 10] [--total-timeout 45]
```

| Exit code | Meaning | Chat status |
|---|---|---|
| 0 | success | SUCCESS |
| 1 | internal error (still returns JSON) | FAILED |
| 2 | invalid input or blocked host (including via redirect) | FAILED / REJECTED |
| 3 | start page unreachable (DNS, timeout, HTTP ≥ 400, not HTML) | FAILED |
| 4 | robots.txt disallows the start URL | FAILED |
| 5 | partial: some pages failed or the time budget ran out | PARTIAL |
| (killed) | backend hard timeout / user cancel | TIMEOUT / CANCELLED |

How it works:
- **Exactly one JSON object** on stdout, even on failure; human-readable logs on stderr.
- **SSRF protection in three places:**
  - a pre-check on the start URL
  - a re-check on **every redirect hop** (redirects are followed manually)
  - a **connect-time guard**: the process wraps `socket.getaddrinfo`, so the addresses checked are exactly the ones the socket connects to, which closes DNS rebinding
- **Headers first:** error pages and non-HTML responses are dropped from their headers alone, without downloading the body.
- **Crawl rules:** honours robots.txt, stays on the same domain, limits each page to 2 MB, pauses 0.3s between requests, and skips duplicate content by SHA-256 of the body.
- **Security flags:** it marks hidden text (`display:none`, `visibility:hidden`, `hidden`, `aria-hidden`) and instruction-like phrases such as "ignore previous instructions", "reveal … API key" or `curl … | sh`.
- **Time budget:** the whole run has a 45s budget, below the backend's 60s hard kill, so it normally ends with a clean partial result instead of being killed.

---

## Measured on real runs (16 Nov 2026, Windows 11, home connection)

| Request | Intent | Crawl (CLI) | Summary | Total | Evidence |
|---|---|---|---|---|---|
| example.com, mandatory prompt | 1.52s | 1.21s | 2.41s | ~5.2s | 3/3 verified |
| books.toscrape.com, 3 pages | 1.99s | 4.37s | 22.84s* | 29.2s | 10/10 verified |
| injection test pages, 2 pages | 1.57s | 1.13s | 3.42s | 6.1s | 4/4 verified, 1 flag |
| metadata IP (rejected) | 4.20s | not run | not run | 4.2s | n/a |

\* The first models in the chain were slow or overloaded at that moment, so the summary fell through to `gemini-3-flash-preview`. The audit file records which model answered. The CLI itself was never the bottleneck.

---

## What broke during development, and what we changed

1. **The page limit was used up by duplicates.** books.toscrape.com serves the same page at `/` and `/index.html`, so one of five page slots was wasted. Pages are now skipped when their body has the same SHA-256 as a page already crawled.
2. **`gemini-2.5-flash` is listed but not usable.** The models endpoint lists it, yet it returns 404 "no longer available to new users", and `flash-latest` returned 503. Four test prompts took over 2 minutes before falling back, though the rule-based parser still handled all four correctly. Response times of candidate models were measured and the result is a fixed chain with a 20s per-call and 45s per-step budget.
3. **Truncated titles.** The visible text shows "A Light in the ...", but the full title is in the link's `title` attribute. Link texts are now sent to the model and used during evidence grading.
4. **A hole in the evidence check.** It only checked that the quote existed, so a real quote with a wrong value would have shown as verified. It's now three levels, with a test for exactly that case.
5. **Untested security claims.** Redirect-hop SSRF checking was in the README before any test exercised it. The local test server now covers it, plus off-prefix redirects and redirect loops.
6. **`subprocess.run` couldn't stream or cancel.** It returns only when the process ends. It was replaced by `Popen` with reader threads and a watchdog. The guarantees (`shell=False`, timeout, minimal environment) are unchanged.
7. **DNS rebinding was a listed limitation.** It's now closed by the connect-time guard described above.
8. **Smaller fixes:**
   - titles contained raw newlines
   - example.com's page changed (no `<h1>`, no links), which made it a thin demo, so books.toscrape.com was added
   - `load_dotenv()` fails when Python reads its script from stdin, so the `.env` path is now explicit

---

## Design decisions (and what we rejected)

| Decision | Alternatives considered | Why |
|---|---|---|
| A purpose-built crawler CLI | Scrapy, `wget --mirror`, Playwright, a general agent CLI | We control the contract: exit codes, one JSON object, hard limits, robots.txt. Playwright is heavy for static sites. An agent CLI gives the model far more power than "crawl this URL". |
| Gemini over plain REST (`requests`) | `google-genai` SDK | No extra dependency, explicit timeouts and retries, and the key goes in a header that never appears in a URL or log. |
| `Popen` + watchdog | `subprocess.run`, asyncio subprocess | Live stderr and cancel with the fewest moving parts. Works the same on Windows and POSIX. |
| Exit codes + JSON contract | Parsing the CLI's log text | Status is decided by evidence the tool has to produce, not by guessing from text. |
| Validate in the backend **and** in the CLI | Validate once | The backend gives clear early errors; the CLI protects itself even if called directly. |
| The server builds the JSON | Let the LLM write the JSON | The LLM only supplies summary text and facts; structure, provenance and validity are guaranteed by code. |
| SSE over a POST `fetch` stream | WebSocket, `EventSource` | `EventSource` is GET-only, and WebSocket is more than this one-way stream needs. |
| Sequential crawling | Concurrent requests | Kinder to the target site, deterministic order, and the page limits are small anyway. |

---

## Security summary

| Risk | Mitigation |
|---|---|
| Shell injection | No shell anywhere. `Popen(list, shell=False)`; argv built only from validated, typed fields; the URL always starts with `http(s)://` so it can't be read as a flag. |
| Arbitrary commands | Fixed tool allowlist (`TOOL_REGISTRY`). The LLM fills parameters and cannot choose commands. |
| SSRF | Public-IP-only checks in the validator, in the CLI, on every redirect hop, and at connect time. |
| Runaway tasks | Page, depth, byte, per-request and total-time limits; 60s hard kill; user cancel; 8 MB stdout cap; at most 2 concurrent runs. |
| Prompt injection | Untrusted-content tags with spoofed tags stripped, a model with no tools, URL taken only from the user, crawler-side flags, and facts graded against the crawl. |
| Secret leakage | Key only in `.env` (git-ignored), sent in a header, never passed to the CLI process, redacted from captured logs. |
| False success | Exit code **plus** validated JSON; mismatches or empty "success" are discarded; partial results and AI fallbacks are labelled. |
| XSS from crawled pages | The UI never uses `innerHTML` for data. |

**Known limitations**
- Content that only appears after JavaScript runs isn't seen, because there's no browser.
- Injection detection is pattern-based. It's a warning signal, not a guarantee; the real protection is that the model has no tools and its facts are graded.
- Each page's text is trimmed to 5,000 characters, and about 24,000 characters go to the model.
- In `ALLOW_TESTPAGES` mode the connect-time guard allows `127.0.0.1`. URL-level checks still restrict crawling to the `/testpages/` prefix.

**With more time**
- Two-stage shutdown (SIGTERM, or CTRL_BREAK on Windows, then kill) so cancel returns partial data.
- Honour robots `Crawl-delay`, `rel=nofollow` and `<base href>`.
- An optional Playwright mode for JavaScript sites behind the same exit-code contract.
- A Pydantic model and published JSON Schema for the CLI output.
- CI on Windows and Linux.

---

## Project structure
```
cli/crawler_cli.py        the approved CLI tool (standalone)
backend/validator.py      Pydantic task model + SSRF pre-check
backend/runner.py         tool allowlist, Popen execution, capture, watchdog, status interpretation
backend/llm.py            minimal Gemini REST client: timeouts, retries, model fallback chain
backend/intent.py         NL message -> structured intent (LLM + rule-based fallback)
backend/summarizer.py     summary + facts, untrusted-content handling, evidence grading
backend/orchestrator.py   pipeline, step timings, audit trace, final JSON builder
backend/main.py           FastAPI: UI, SSE stream, JSON endpoint, cancel, health
static/index.html         single-file terminal-style UI (vanilla JS)
testpages/                local prompt-injection demo pages (served only with ALLOW_TESTPAGES=1)
tests/test_offline.py     34 checks, including a local HTTP server for redirect/SSRF cases
docs/                     screenshots and a real sample output
```

API: `POST /api/chat/stream` (SSE) · `POST /api/chat` (JSON, handy for curl) · `POST /api/cancel/{run_id}` · `GET /api/health` · `GET /docs`.

---

## Tech stack, AI tools and external services

- **Backend:** Python 3.10+, FastAPI, Uvicorn, Pydantic, python-dotenv (exact versions pinned in `requirements.txt`)
- **CLI crawler:** requests, BeautifulSoup (`html.parser`), plus Python standard library modules (`argparse`, `ipaddress`, `urllib.robotparser`)
- **Frontend:** one HTML file with vanilla JavaScript; the terminal look uses the Windows Terminal "Campbell" colour scheme
- **Runtime AI service:** **Google Gemini API**, used only to turn the message into parameters and to write the summary and facts. It never runs commands.
- **AI coding assistant:** **Claude Code (Anthropic)** wrote most of the implementation, phase by phase. The participant set the architecture, the stack, the dependency allowlist and the core security rules (`shell=False`, SSRF blocking, untrusted page content, evidence-backed JSON). Claude Code proposed and implemented the detailed design within those rules, such as the exit-code scheme, Popen streaming, the Gemini fallback chain, the test pages and evidence grading. Every phase was tested end to end.
- **Demo sites:** https://example.com and https://books.toscrape.com, a public sandbox built for scraping practice.
