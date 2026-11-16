"""runner.py - starts an APPROVED CLI tool as a subprocess and captures what really happened.

Flow:  CrawlTask -> build_command() -> subprocess.run(list, shell=False, timeout) ->
       capture stdout / stderr / exit code -> interpret() -> RunResult (+ audit log in runs/)

Terminal test:
  python -m backend.runner --url https://example.com
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from backend.validator import CrawlTask, InvalidRequest, validate_task

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
STDERR_TAIL_CHARS = 4000

# Allowlist: the backend can only ever run tools listed here.
TOOL_REGISTRY = {
    "site_crawler": {
        "script": ROOT / "cli" / "crawler_cli.py",
        "timeout": 60,          # hard kill after this many seconds
        "crawl_budget": 45,     # passed to the CLI so it finishes cleanly before the hard kill
        "description": "Crawl a public website (same domain, robots.txt honored) and return page data as JSON.",
    },
}

# Meaning of each crawler exit code -> (status, human reason)
EXIT_CODES = {
    0: ("success", "crawl completed"),
    1: ("failed", "the crawler hit an internal error"),
    2: ("failed", "the request was rejected as invalid"),
    3: ("failed", "the website could not be reached"),
    4: ("failed", "the website's robots.txt does not allow crawling this page"),
    5: ("partial", "only part of the site could be crawled"),
}

# Only these environment variables are passed to the child; API keys and other secrets are not.
SAFE_ENV_VARS = ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "HOME", "LANG", "LC_ALL")
SECRET_ENV_VARS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


@dataclass
class RunResult:
    run_id: str
    tool: str
    command: list
    status: str                 # success | partial | failed | timeout
    reason: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    started_at: str
    data: dict | None = None    # parsed CLI JSON (only if it passed validation)
    stderr: str = ""            # tail of the CLI log
    log_file: str | None = None
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def build_command(tool, task: CrawlTask):
    """Build the argument LIST from validated fields only. No shell, no string concatenation."""
    if tool not in TOOL_REGISTRY:
        raise InvalidRequest(f"tool '{tool}' is not in the approved tool registry")
    spec = TOOL_REGISTRY[tool]
    return [
        sys.executable, "-I",              # -I: isolated mode, ignores PYTHON* env vars and user site-packages
        str(spec["script"]),
        "--url", task.url,                 # validated: always starts with http(s)://, so it can't be read as a flag
        "--max-pages", str(task.max_pages),
        "--max-depth", str(task.max_depth),
        "--total-timeout", str(spec["crawl_budget"]),
    ]


def safe_env():
    return {k: os.environ[k] for k in SAFE_ENV_VARS if k in os.environ}


def redact(text):
    """Belt and braces: never let a secret value appear in logs or chat output."""
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) > 6:
            text = text.replace(value, f"[REDACTED {name}]")
    return text


def interpret(exit_code, timed_out, stdout, timeout):
    """Decide the honest status from the evidence: exit code + parsed, validated JSON."""
    if timed_out:
        return "timeout", f"the crawler did not finish within {timeout}s and was stopped", None, []
    try:
        data = json.loads(stdout)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except ValueError:
        return "failed", f"the crawler exited with code {exit_code} but did not return valid JSON", None, []

    warnings = []
    for key, typ in (("pages", list), ("errors", list), ("status", str)):
        if not isinstance(data.get(key), typ):
            return "failed", f"crawler output is missing a valid '{key}' field", None, []
    if data.get("exit_code") != exit_code:
        return "failed", "crawler output does not match its exit code; result discarded", None, []

    status, reason = EXIT_CODES.get(exit_code, ("failed", f"the crawler exited with unknown code {exit_code}"))
    if status == "success" and not data["pages"]:
        return "failed", "the crawler reported success but returned no pages; result discarded", None, []
    if status != "success" and isinstance(data.get("error"), dict) and data["error"].get("message"):
        reason = f"{reason}: {data['error']['message']}"
    if status == "partial":
        stats = data.get("stats", {})
        bits = []
        if stats.get("page_errors"):
            bits.append(f"{stats['page_errors']} page(s) failed")
        if stats.get("budget_exhausted"):
            bits.append("time budget ran out")
        if bits:
            reason = f"{reason} ({', '.join(bits)})"
    if data.get("clamped"):
        warnings.append("some limits were reduced to the maximum allowed: " + ", ".join(data["clamped"]))
    return status, reason, data, warnings


def save_log(result: RunResult):
    try:
        RUNS_DIR.mkdir(exist_ok=True)
        path = RUNS_DIR / f"{result.run_id}.json"
        path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        return str(path.relative_to(ROOT))
    except OSError:
        return None


def run_tool(tool, task: CrawlTask, timeout=None):
    """Run an approved tool and return a RunResult. Never raises for tool failures."""
    cmd = build_command(tool, task)
    timeout = timeout or TOOL_REGISTRY[tool]["timeout"]
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.monotonic()
    timed_out, exit_code = False, None

    try:
        proc = subprocess.run(
            cmd, shell=False, capture_output=True, timeout=timeout,
            env=safe_env(), cwd=ROOT, stdin=subprocess.DEVNULL,
        )
        stdout_b, stderr_b, exit_code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:   # subprocess.run has already killed the child
        stdout_b, stderr_b, timed_out = e.stdout or b"", e.stderr or b"", True
    except OSError as e:
        stdout_b, stderr_b = b"", f"could not start tool: {type(e).__name__}".encode()

    duration_ms = int((time.monotonic() - t0) * 1000)
    stdout = redact(stdout_b.decode("utf-8", errors="replace"))
    stderr = redact(stderr_b.decode("utf-8", errors="replace"))

    if exit_code is None and not timed_out:
        status, reason, data, warnings = "failed", "the crawler process could not be started", None, []
    else:
        status, reason, data, warnings = interpret(exit_code, timed_out, stdout, timeout)

    result = RunResult(
        run_id=run_id, tool=tool,
        command=[Path(cmd[0]).name] + [str(Path(c).relative_to(ROOT)) if c.startswith(str(ROOT)) else c for c in cmd[1:]],
        status=status, reason=reason, exit_code=exit_code, timed_out=timed_out,
        duration_ms=duration_ms, started_at=started_at, data=data,
        stderr=stderr[-STDERR_TAIL_CHARS:], warnings=warnings,
    )
    result.log_file = save_log(result)
    return result


def _main():
    ap = argparse.ArgumentParser(description="Validate a crawl request and run the approved crawler CLI.")
    ap.add_argument("--url", required=True)
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument("--max-depth", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=None, help="override the hard kill timeout (seconds)")
    a = ap.parse_args()

    try:
        task = validate_task({"url": a.url, "max_pages": a.max_pages, "max_depth": a.max_depth})
    except InvalidRequest as e:
        print(f"REJECTED before execution: {e}")
        return 2

    r = run_tool("site_crawler", task, timeout=a.timeout)
    print(f"command   : {' '.join(r.command)}")
    print(f"status    : {r.status.upper()}  -  {r.reason}")
    print(f"exit code : {r.exit_code}   timed out: {r.timed_out}   duration: {r.duration_ms} ms")
    if r.data:
        print(f"pages     : {[p['url'] for p in r.data['pages']]}")
    for w in r.warnings:
        print(f"warning   : {w}")
    print(f"log file  : {r.log_file}")
    print("--- crawler stderr (tail) ---")
    print(r.stderr.strip()[-1500:])
    return 0 if r.status in ("success", "partial") else 1


if __name__ == "__main__":
    sys.exit(_main())
