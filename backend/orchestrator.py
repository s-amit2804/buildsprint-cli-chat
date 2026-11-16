"""orchestrator.py - the chat pipeline, step by step, emitting events for the live timeline.

  message -> intent (LLM, or rule-based fallback) -> plan -> validate -> run approved CLI (live logs)
          -> check evidence of success -> summarise (LLM, untrusted data) -> verify evidence -> final JSON

emit(event, payload) is called at every step; the API turns these into Server-Sent Events.
"""

import json

from backend import llm
from backend.intent import parse_intent
from backend.runner import TOOL_REGISTRY, build_command, run_tool
from backend.summarizer import summarize
from backend.validator import InvalidRequest, validate_task

TOOL = "site_crawler"


def _build_output_json(task, run, summ):
    """The structured JSON the user asked for. Built by code from the crawl + verified LLM output."""
    data = run.data
    first = data["pages"][0]
    out = {
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
            {k: x[k] for k in ("field", "value", "source_url", "evidence", "verified")} for x in summ["extracted"]
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
        "execution": {
            "tool": run.tool,
            "run_id": run.run_id,
            "exit_code": run.exit_code,
            "duration_ms": run.duration_ms,
            "pages_crawled": len(data["pages"]),
            "robots_txt": data.get("robots", {}).get("hosts", {}),
            "robots_skipped_urls": data.get("robots", {}).get("skipped_urls", []),
        },
        "provenance": {
            "summary_generated_by": summ["model"] or "rule-based fallback",
            "evidence_verified": f"{summ['verified_count']}/{len(summ['extracted'])}",
        },
    }
    json.loads(json.dumps(out))  # guarantee it serialises to valid JSON
    return out


def handle_message(message, emit, cancel_event=None, run_id=None):
    """Runs the whole pipeline. Always ends with exactly one 'final' event and returns its payload."""

    def finish(payload):
        emit("final", payload)
        return payload

    message = (message or "").strip()
    if not message:
        return finish({"kind": "error", "status": "failed", "message": "Please type a request."})
    if len(message) > 4000:
        return finish({"kind": "error", "status": "failed", "message": "Message is too long (max 4000 characters)."})

    # 1. Understand the request
    emit("step", {"id": "intent", "label": "Understanding your request", "state": "running"})
    intent = parse_intent(message)
    emit("step", {"id": "intent", "label": "Understanding your request", "state": "done",
                  "detail": f"via {intent.model or 'rule-based fallback'}"})

    plan = {
        "task": intent.task, "url": intent.url, "max_pages": intent.max_pages, "max_depth": intent.max_depth,
        "wants_summary": intent.wants_summary, "wants_json": intent.wants_json,
        "fields_requested": intent.fields_requested, "explanation": intent.explanation,
        "parsed_by": intent.model or "rule-based fallback", "notes": intent.notes,
    }
    emit("plan", plan)

    if intent.task != "crawl_site":
        reason = "; ".join(intent.notes) if intent.notes else intent.explanation
        return finish({
            "kind": "clarify", "status": "not_run",
            "message": "I can crawl and summarise a public website. Please include a URL, for example: "
                       "\"Crawl https://books.toscrape.com and return book titles and prices as JSON\".",
            "detail": reason, "plan": plan,
        })

    # 2. Validate (the only path to the CLI)
    emit("step", {"id": "validate", "label": "Validating URL and limits", "state": "running"})
    try:
        task = validate_task({"url": intent.url, "max_pages": intent.max_pages,
                              "max_depth": intent.max_depth, "wants_json": intent.wants_json})
    except InvalidRequest as e:
        emit("step", {"id": "validate", "label": "Validating URL and limits", "state": "failed", "detail": str(e)})
        return finish({"kind": "error", "status": "rejected", "plan": plan,
                       "message": f"Request rejected before execution: {e}"})
    emit("step", {"id": "validate", "label": "Validating URL and limits", "state": "done",
                  "detail": f"{task.url} | max_pages={task.max_pages} max_depth={task.max_depth}"})

    # 3. Execute the approved CLI
    emit("step", {"id": "execute", "label": f"Running CLI tool '{TOOL}'", "state": "running"})
    shown =["python"] + [a.split("\\")[-1] if a.endswith(".py") else a for a in build_command(TOOL, task)[2:]]
    emit("command", {"tool": TOOL, "argv": shown, "timeout_s": TOOL_REGISTRY[TOOL]["timeout"], "shell": False})
    run = run_tool(TOOL, task, on_log=lambda line: emit("log", {"line": line}),
                   cancel_event=cancel_event, run_id=run_id)
    execution = {"run_id": run.run_id, "status": run.status, "reason": run.reason, "exit_code": run.exit_code,
                 "timed_out": run.timed_out, "duration_ms": run.duration_ms, "log_file": run.log_file,
                 "warnings": run.warnings}
    emit("step", {"id": "execute", "label": f"Running CLI tool '{TOOL}'",
                  "state": "done" if run.status in ("success", "partial") else "failed",
                  "detail": f"exit code {run.exit_code} | {run.duration_ms} ms | {run.status}"})
    emit("execution", execution)

    if run.status not in ("success", "partial"):
        return finish({"kind": "result", "status": run.status, "plan": plan, "execution": execution,
                       "message": f"The crawl did not succeed: {run.reason}.", "stderr": run.stderr[-3000:]})

    # 4. Summarise + verify
    emit("step", {"id": "summarize", "label": "Summarising (page content treated as untrusted)", "state": "running"})
    summ = summarize(run.data, message, intent.fields_requested)
    emit("step", {"id": "summarize", "label": "Summarising (page content treated as untrusted)", "state": "done",
                  "detail": f"via {summ['model'] or 'rule-based fallback'}"})
    emit("step", {"id": "verify", "label": "Verifying evidence against crawled text", "state": "done",
                  "detail": f"{summ['verified_count']}/{len(summ['extracted'])} snippets found verbatim"})

    output = _build_output_json(task, run, summ)
    n = len(run.data["pages"])
    evidence = f"{n} page(s) crawled by the CLI in {run.duration_ms / 1000:.1f}s, exit code {run.exit_code}"
    message = evidence if run.status == "success" else f"{run.reason}. {evidence}"
    return finish({
        "kind": "result", "status": run.status, "plan": plan, "execution": execution,
        "message": message, "summary": summ["summary"], "key_points": summ["key_points"],
        # JSON is always produced (it's the evidence record); the UI expands it when the user asked for JSON.
        "extracted": summ["extracted"], "summary_note": summ["note"], "json": output, "json_requested": intent.wants_json,
        "stderr": run.stderr[-3000:],
    })


def health():
    return {"llm_configured": llm.is_configured(), "models": llm.model_chain(), "tools": list(TOOL_REGISTRY)}
