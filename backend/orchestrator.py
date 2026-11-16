"""orchestrator.py - the chat pipeline, step by step, emitting events for the live timeline.

  message -> intent (LLM, or rule-based fallback) -> plan -> validate -> run approved CLI (live logs)
          -> check evidence of success -> summarise (LLM, untrusted data) -> verify evidence -> final JSON

emit(event, payload) is called at every step; the API turns these into Server-Sent Events.
Every request (including rejected ones) leaves one audit file: runs/<run_id>.json.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from backend import llm
from backend.intent import parse_intent
from backend.runner import RUNS_DIR, ROOT, TOOL_REGISTRY, build_command, new_run_id, run_tool
from backend.summarizer import summarize
from backend.validator import InvalidRequest, validate_task

TOOL = "site_crawler"


class Trace:
    """Wraps emit(): times each step and records every event for the audit file."""

    def __init__(self, emit, run_id, message):
        self._emit, self.run_id, self.message = emit, run_id, message
        self.t0 = time.monotonic()
        self.events, self.timings, self._started = [], {}, {}
        self.execution = None

    def elapsed(self):
        return int((time.monotonic() - self.t0) * 1000)

    def emit(self, event, payload):
        if event != "log":  # stderr lines are kept once, in execution.stderr
            self.events.append({"t_ms": self.elapsed(), "event": event, "data": payload})
        self._emit(event, payload)

    def start(self, step_id, label):
        self._started[step_id] = time.monotonic()
        self.emit("step", {"id": step_id, "label": label, "state": "running"})

    def end(self, step_id, label, detail=None, state="done"):
        ms = int((time.monotonic() - self._started.get(step_id, time.monotonic())) * 1000)
        self.timings[step_id] = ms
        self.emit("step", {"id": step_id, "label": label, "state": state, "detail": detail, "ms": ms})

    def save(self, final):
        record = {
            "run_id": self.run_id,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "message": self.message[:4000],
            "status": final.get("status"),
            "total_ms": self.elapsed(),
            "timings_ms": self.timings,
            "execution": self.execution,
            "events": self.events,
        }
        try:
            RUNS_DIR.mkdir(exist_ok=True)
            path = RUNS_DIR / f"{self.run_id}.json"
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            return str(path.relative_to(ROOT))
        except OSError:
            return None


def _security_flags(data):
    flags = []
    for p in data["pages"]:
        for f in p.get("security_flags", []):
            flags.append({"page": p["url"], **f})
    return flags


def _build_output_json(task, run, summ, flags, timings):
    """The structured JSON the user asked for. Built by code from the crawl + verified LLM output."""
    data = run.data
    first = data["pages"][0]
    out = {
        "schema_version": "1.1",
        "status": run.status,
        "request": {"url": task.url, "max_pages": task.max_pages, "max_depth": task.max_depth},
        "site": {
            "start_url": data.get("start_url"),
            "final_url": data.get("final_start_url"),
            "title": first.get("title"),
            "description": first.get("meta_description") or None,
        },
        "summary": summ["summary"],
        "key_points": summ["key_points"],
        "extracted_data": [
            {k: x[k] for k in ("field", "value", "source_url", "evidence", "verification")} for x in summ["extracted"]
        ],
        "pages": [
            {
                "url": p["url"],
                "title": p.get("title"),
                "meta_description": p.get("meta_description") or None,
                "headings": [h["text"] for h in p.get("headings", [])][:10],
                "word_count": p.get("word_count"),
                "links_found": p.get("links_found"),
            }
            for p in data["pages"]
        ],
        "errors": data.get("errors", []),
        "security": {"flags": flags, "pages_flagged": len({f["page"] for f in flags})},
        "execution": {
            "tool": run.tool,
            "run_id": run.run_id,
            "pid": run.pid,
            "exit_code": run.exit_code,
            "duration_ms": run.duration_ms,
            "stdout_bytes": run.stdout_bytes,
            "stdout_sha256": run.stdout_sha256,
            "pages_crawled": len(data["pages"]),
            "robots_txt": data.get("robots", {}).get("hosts", {}),
            "robots_skipped_urls": data.get("robots", {}).get("skipped_urls", []),
        },
        "provenance": {
            "intent_parsed_by": None,
            "summary_generated_by": summ["model"] or "rule-based fallback",
            "evidence": {
                "verified": summ["verified_count"],
                "paraphrased": summ["paraphrased_count"],
                "unverified": len(summ["extracted"]) - summ["verified_count"] - summ["paraphrased_count"],
            },
            "timings_ms": timings,
        },
    }
    json.loads(json.dumps(out))  # guarantee it serialises to valid JSON
    return out


def handle_message(message, emit, cancel_event=None, run_id=None):
    """Runs the whole pipeline. Always ends with exactly one 'final' event and returns its payload."""
    message = (message or "").strip()
    trace = Trace(emit, run_id or new_run_id(), message)

    def finish(payload):
        payload["run_id"] = trace.run_id
        payload["total_ms"] = trace.elapsed()
        payload["audit_file"] = trace.save(payload)
        trace.emit("final", payload)
        return payload

    if not message:
        return finish({"kind": "error", "status": "failed", "message": "Please type a request."})
    if len(message) > 4000:
        return finish({"kind": "error", "status": "failed", "message": "Message is too long (max 4000 characters)."})

    # 1. Understand the request
    trace.start("intent", "understand request")
    intent = parse_intent(message)
    trace.end("intent", "understand request", f"via {intent.model or 'rule-based fallback'}")

    plan = {
        "task": intent.task, "url": intent.url, "max_pages": intent.max_pages, "max_depth": intent.max_depth,
        "wants_summary": intent.wants_summary, "wants_json": intent.wants_json,
        "fields_requested": intent.fields_requested, "explanation": intent.explanation,
        "parsed_by": intent.model or "rule-based fallback", "notes": intent.notes,
    }
    trace.emit("plan", plan)

    if intent.task != "crawl_site":
        reason = "; ".join(intent.notes) if intent.notes else intent.explanation
        return finish({
            "kind": "clarify", "status": "not_run",
            "message": "I can crawl and summarise a public website. Please include a URL, for example: "
                       "\"Crawl https://books.toscrape.com and return book titles and prices as JSON\".",
            "detail": reason, "plan": plan,
        })

    # 2. Validate (the only path to the CLI)
    trace.start("validate", "validate url + limits")
    try:
        task = validate_task({"url": intent.url, "max_pages": intent.max_pages,
                              "max_depth": intent.max_depth, "wants_json": intent.wants_json})
    except InvalidRequest as e:
        trace.end("validate", "validate url + limits", str(e), state="failed")
        return finish({"kind": "error", "status": "rejected", "plan": plan,
                       "message": f"Request rejected before execution: {e}"})
    trace.end("validate", "validate url + limits", f"{task.url}  pages<={task.max_pages}  depth<={task.max_depth}")

    # 3. Execute the approved CLI
    trace.start("execute", f"run cli tool '{TOOL}'")
    argv = build_command(TOOL, task)
    shown = ["python"] + [Path(a).name if a.endswith(".py") else a for a in argv[2:]]
    trace.emit("command", {"tool": TOOL, "argv": shown, "timeout_s": TOOL_REGISTRY[TOOL]["timeout"], "shell": False})
    run = run_tool(TOOL, task, on_log=lambda line, ms: trace.emit("log", {"line": line, "t_ms": ms}),
                   cancel_event=cancel_event, run_id=trace.run_id, save=False)
    trace.execution = run.to_dict()
    execution = {"run_id": run.run_id, "status": run.status, "reason": run.reason, "exit_code": run.exit_code,
                 "timed_out": run.timed_out, "duration_ms": run.duration_ms, "pid": run.pid,
                 "stdout_bytes": run.stdout_bytes, "stdout_sha256": run.stdout_sha256, "warnings": run.warnings}
    ok = run.status in ("success", "partial")
    trace.end("execute", f"run cli tool '{TOOL}'",
              f"pid {run.pid}  exit {run.exit_code}  {run.stdout_bytes} B stdout  {run.status}",
              state="done" if ok else "failed")
    trace.emit("execution", execution)

    if not ok:
        return finish({"kind": "result", "status": run.status, "plan": plan, "execution": execution,
                       "message": f"The crawl did not succeed: {run.reason}.", "stderr": run.stderr[-3000:]})

    flags = _security_flags(run.data)
    if flags:
        trace.emit("security", {"flags": flags})

    # 4. Summarise + verify
    trace.start("summarize", "summarise (page text = untrusted data)")
    summ = summarize(run.data, message, intent.fields_requested)
    trace.end("summarize", "summarise (page text = untrusted data)", f"via {summ['model'] or 'rule-based fallback'}")
    trace.start("verify", "verify evidence against crawl")
    n_ext = len(summ["extracted"])
    trace.end("verify", "verify evidence against crawl",
              f"{summ['verified_count']} verified, {summ['paraphrased_count']} paraphrased, "
              f"{n_ext - summ['verified_count'] - summ['paraphrased_count']} unverified")

    output = _build_output_json(task, run, summ, flags, dict(trace.timings))
    output["provenance"]["intent_parsed_by"] = plan["parsed_by"]
    n = len(run.data["pages"])
    evidence = f"{n} page(s) crawled by the CLI in {run.duration_ms / 1000:.1f}s, exit code {run.exit_code}"
    msg = evidence if run.status == "success" else f"{run.reason}. {evidence}"
    return finish({
        "kind": "result", "status": run.status, "plan": plan, "execution": execution,
        "message": msg, "summary": summ["summary"], "key_points": summ["key_points"],
        # JSON is always produced (it's the evidence record); the UI expands it when the user asked for JSON.
        "extracted": summ["extracted"], "summary_note": summ["note"], "json": output, "json_requested": intent.wants_json,
        "security_flags": flags, "timings_ms": dict(trace.timings), "stderr": run.stderr[-3000:],
    })


def health():
    return {"llm_configured": llm.is_configured(), "models": llm.model_chain(), "tools": list(TOOL_REGISTRY)}
