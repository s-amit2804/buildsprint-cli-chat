# Plan

## Goal
Chat app where a natural-language request makes the backend run a real CLI crawler,
capture stdout/stderr/exit code, validate, and return an honest summary + valid JSON.
Deadline: 16 Nov 2026, 11:59 PM IST. Build locally; no deployment.

## Architecture
Browser chat UI -> FastAPI POST /chat
1. intent.py: LLM -> strict JSON (task, url, max_pages, wants_json); regex fallback
2. validator.py: http/https only, block localhost/private/link-local IPs (incl. 169.254.169.254), cap limits
3. runner.py: subprocess.run([...], shell=False, timeout=60), capture stdout/stderr/exit code
4. cli/crawler_cli.py: requests + BeautifulSoup, robots.txt, same-domain, JSON on stdout, logs on stderr
5. output processing: check exit code, parse + validate JSON, truncate
6. summarizer.py: LLM summary; page text is untrusted data, no tools
7. static/index.html: chat UI with status badge, summary, JSON block, execution log

## Exit codes
0 success, 2 invalid input, 3 unreachable, 4 blocked by robots.txt, 5 partial

## Differentiators
- Evidence-backed JSON: each field has value, source_url, evidence; backend verifies snippets against crawled text
- Plan preview + live execution timeline (SSE)
- Tool registry allowlist + prompt-injection demo page in testpages/injection.html

## Phases (stop for review after each)
1. cli/crawler_cli.py, tested alone in terminal
2. runner.py + validator.py
3. intent.py + summarizer.py
4. main.py + chat UI
5. evidence verification, timeline, injection demo
6. README (setup, CLI used, AI tools used) and demo video

## Rules
- No raw user/LLM text in a shell
- Secrets only in .env (gitignored)
- Honest statuses: success / partial / failed / timeout
- Demo sites: https://example.com, https://books.toscrape.com
## Status (2026-11-16)
All phases built and tested. Decisions made during the build:
- LLM: Google Gemini via plain REST (`requests`, no SDK). Model chain gemini-3.5-flash-lite -> gemini-3.1-flash-lite -> gemini-3-flash-preview
  (gemini-2.5-flash is closed to new keys). Rule-based fallbacks when the AI is unavailable, always labelled.
- Runner uses Popen (not subprocess.run) to stream stderr live and support cancel; same shell=False / timeout / minimal env.
- Prompt-injection demo pages served only when ALLOW_TESTPAGES=1, and only the exact /testpages/ prefix is exempt from the SSRF guard.
- Exit code 1 added for internal crawler errors.
See README.md for setup and docs/VIDEO_SCRIPT.md for the recording plan.
