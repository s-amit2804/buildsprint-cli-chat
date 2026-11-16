"""Offline checks (no network, no API key needed).  Run:  python -m tests.test_offline"""

import json
import os
import subprocess
import sys

os.environ.pop("ALLOW_TESTPAGES", None)

from backend.runner import build_command, interpret, redact, safe_env  # noqa: E402
from backend.summarizer import verify_evidence  # noqa: E402
from backend.validator import InvalidRequest, validate_task  # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print(f"FAIL  {name}")
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


def rejected(data):
    try:
        validate_task(data)
        return False
    except InvalidRequest:
        return True


# validator
check("metadata IP rejected", rejected({"url": "http://169.254.169.254/latest"}))
check("localhost rejected", rejected({"url": "http://localhost:8000"}))
check("private IP rejected", rejected({"url": "http://192.168.0.10"}))
check("IPv6 loopback rejected", rejected({"url": "http://[::1]/"}))
check("ftp rejected", rejected({"url": "ftp://example.org"}))
check("credentials rejected", rejected({"url": "https://a:b@example.org"}))
check("unknown field rejected", rejected({"url": "https://example.org", "cmd": "rm -rf /"}))
t = validate_task({"url": "https://example.org", "max_pages": 999, "max_depth": -1})
check("limits clamped", (t.max_pages, t.max_depth) == (20, 0))

# runner: argument list, no shell, no secrets
cmd = build_command("site_crawler", t)
check("command is a list with --url as separate arg", isinstance(cmd, list) and cmd[cmd.index("--url") + 1] == t.url)
try:
    build_command("bash", t)
    refused = False
except InvalidRequest:
    refused = True
check("unapproved tool refused", refused)
os.environ["GEMINI_API_KEY"] = "test-secret-value-xyz"
check("API key not passed to child", "GEMINI_API_KEY" not in safe_env())
check("secrets redacted", "test-secret-value-xyz" not in redact("leak test-secret-value-xyz"))

# runner: honest status interpretation
good = {"status": "success", "exit_code": 0, "pages": [{"url": "u"}], "errors": []}
check("valid success accepted", interpret(0, False, json.dumps(good), 60)[0] == "success")
check("timeout reported", interpret(None, True, "", 60)[0] == "timeout")
check("non-JSON stdout -> failed", interpret(0, False, "oops", 60)[0] == "failed")
check("exit-code mismatch -> failed", interpret(5, False, json.dumps(good), 60)[0] == "failed")
check("success without pages -> failed", interpret(0, False, json.dumps(dict(good, pages=[])), 60)[0] == "failed")

# evidence verification
crawl = {"pages": [{"url": "https://s/", "title": "T", "text": "Espresso costs $3.00 today", "headings": [], "links": []}]}
v = verify_evidence([{"field": "p", "value": "$3", "source_url": "https://s/", "evidence": "Espresso costs $3.00"},
                     {"field": "p", "value": "$9", "source_url": "https://s/", "evidence": "Espresso costs $9.00"},
                     {"field": "p", "value": "x", "source_url": "https://other/", "evidence": "Espresso costs $3.00"}], crawl)
check("verbatim evidence verified", v[0]["verified"])
check("invented evidence flagged", not v[1]["verified"])
check("wrong source_url flagged", not v[2]["verified"])

# CLI contract: bad input -> exit 2 and JSON on stdout
p = subprocess.run([sys.executable, "cli/crawler_cli.py", "--url", "http://127.0.0.1/"], capture_output=True, text=True)
check("CLI blocks loopback with exit 2", p.returncode == 2 and json.loads(p.stdout)["error"]["type"] == "invalid_input")
p = subprocess.run([sys.executable, "cli/crawler_cli.py", "--max-pages", "x"], capture_output=True, text=True)
check("CLI bad flags -> exit 2 + JSON", p.returncode == 2 and json.loads(p.stdout)["status"] == "failed")

print(f"\nall {passed} checks passed")
