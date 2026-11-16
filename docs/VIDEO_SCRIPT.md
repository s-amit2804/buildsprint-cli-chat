# Demo video script (target: about 6 minutes)

**Before recording**
- Start the server with test pages on:
  - PowerShell: `$env:ALLOW_TESTPAGES="1"; uvicorn backend.main:app --port 8000`
- Open http://127.0.0.1:8000 in the browser. Have VS Code and a second terminal open too.
- Run the mandatory demo once beforehand, so Gemini and the DNS lookups are warm.
- Make sure `.env` is **not** visible on screen at any point.

---

## 1. Problem and solution (0:00–0:40)
> "Most AI chat apps only generate text. The challenge is to make a chat request do real work: run a CLI process on the backend, capture what actually happened, and return an honest result. My app takes a plain-English request, turns it into a validated task, runs an approved crawler CLI as a real subprocess, checks its exit code and JSON output, and returns a summary plus structured JSON where every fact is backed by evidence."

## 2. Architecture (0:40–1:40)
Show the mermaid diagram in README.md on GitHub, or the table below it.
> "The browser sends the message to FastAPI. The intent layer asks Gemini, in JSON mode, for parameters only: URL, page limit, depth, and the fields I want. The validator is a Pydantic model that blocks private IPs and caps the limits. The runner starts the allowlisted CLI with shell=False and streams its logs back live. The summariser treats page text as untrusted data, and every extracted fact is checked against the crawled text."

## 3. Prompt to CLI task (1:40–2:20)
Open `backend/intent.py`, then `backend/runner.py` at `build_command`.
> "The LLM never writes a command. It fills a fixed schema, and the URL it returns must literally appear in my message. Then `build_command` creates an argument list from the validated fields only, and that list is passed to Popen with shell=False. User text never touches a shell."

## 4. Live run of the mandatory use case (2:20–3:50)
Click **"mandatory demo"** in the UI and talk while it runs:
- "The plan shows what the AI understood."
- "This is the exact CLI invocation: shell=False, 60-second timeout."
- "This is the crawler's live stderr, streamed from the subprocess."
- "Exit code 0, duration, run id. The status comes from evidence, not trust."
- Scroll to **Summary**, **Extracted data** (point at the ✓ verified column) and **JSON**. Click **Copy JSON**.

Then run **"multi-page + extraction"** (books.toscrape.com) to show a richer crawl with titles and prices that are all verified.

Optional: in a terminal, show the same CLI directly:
`python cli/crawler_cli.py --url https://example.com`
Point out that stdout is JSON and stderr is logs, then show `echo $LASTEXITCODE`.

## 5. Error and edge cases (3:50–5:10)
- Click **"blocked: cloud metadata IP"**: rejected **before** anything runs (SSRF).
- Click **"blocked by robots.txt"**: the CLI ran and exited with code 4, and the chat says so honestly.
- Click **"unreachable site"**: exit code 3.
- Start books.toscrape.com with 15 pages, then click **Cancel**: status CANCELLED, process stopped.
- Click **"prompt-injection test page"**:
  > "This page hides an instruction telling the AI to say the site is safe, reveal its API key, and recommend a curl-pipe-to-shell command. The summary ignores it and flags it, and the model has no tools anyway."
- Optional: open `runs/` and show one audit log file.

## 6. Stack and AI disclosure (5:10–5:50)
> "Python, FastAPI, Pydantic, requests and BeautifulSoup for the CLI, and a single vanilla-JS page. At runtime I use the Google Gemini API, only for parameters and summaries. I built it with Claude Code as my AI coding assistant. I set the architecture and security rules, it implemented the code phase by phase, and I tested every phase. Thank you."

---

## One-line explanations per file (if asked)
| File | Say this |
|---|---|
| `cli/crawler_cli.py` | "The only tool the backend can run. Typed, capped flags; one JSON object on stdout; logs on stderr; a distinct exit code for every outcome; SSRF and robots.txt checks on every request." |
| `backend/validator.py` | "Everything from the user or the LLM goes through a strict Pydantic model: public http(s) URLs only, unknown fields rejected, limits clamped." |
| `backend/runner.py` | "Allowlisted tool, argument list, shell=False, no secrets in the child's environment, live stderr, hard timeout and cancel, and a status derived from the exit code plus validated JSON." |
| `backend/intent.py` | "Gemini fills a fixed schema. The URL must appear in the user's message, and a regex fallback keeps it working if the AI is down." |
| `backend/summarizer.py` | "Page text is untrusted and tagged. Gemini returns facts with verbatim evidence, and the server verifies each snippet against the crawl." |
| `backend/orchestrator.py` + `main.py` | "Runs the pipeline and streams each step to the browser as server-sent events. Builds the final JSON from real crawl data." |
