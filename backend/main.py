"""main.py - FastAPI app: serves the chat UI and streams pipeline events (Server-Sent Events).

Run:  uvicorn backend.main:app --port 8000
"""

import json
import queue
import threading
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from backend.orchestrator import handle_message, health  # noqa: E402
from backend.runner import new_run_id  # noqa: E402
from backend.validator import testpages_prefix  # noqa: E402

MAX_CONCURRENT_RUNS = 2

app = FastAPI(title="BuildSprint CLI Chat", docs_url="/docs")
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)
_cancel_events: dict[str, threading.Event] = {}

if testpages_prefix():  # opt-in demo pages for the prompt-injection test (ALLOW_TESTPAGES=1)
    app.mount("/testpages", StaticFiles(directory=ROOT / "testpages", html=True), name="testpages")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def api_health():
    return {**health(), "testpages_enabled": bool(testpages_prefix())}


@app.post("/api/chat")
def chat(req: ChatRequest):
    """Non-streaming variant (handy for curl / tests). Returns the final payload plus all events."""
    if not _slots.acquire(blocking=False):
        raise HTTPException(429, "Too many crawls running; try again in a moment.")
    events = []
    try:
        final = handle_message(req.message, lambda e, p: events.append({"event": e, "data": p}))
    finally:
        _slots.release()
    return {"final": final, "events": events}


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest):
    """Streams pipeline events as SSE: step, plan, command, log, execution, final."""
    run_id = new_run_id()
    cancel = threading.Event()
    q: queue.Queue = queue.Queue()

    def sse(event, payload):
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n"

    def worker():
        if not _slots.acquire(blocking=False):
            q.put(("final", {"kind": "error", "status": "failed",
                             "message": "Too many crawls are running right now; please try again in a moment."}))
            q.put(None)
            return
        _cancel_events[run_id] = cancel
        try:
            handle_message(req.message, lambda e, p: q.put((e, p)), cancel_event=cancel, run_id=run_id)
        except Exception as e:  # never leave the client hanging; never leak internals
            q.put(("final", {"kind": "error", "status": "failed", "message": f"Internal error ({type(e).__name__})."}))
        finally:
            _cancel_events.pop(run_id, None)
            _slots.release()
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        yield sse("start", {"run_id": run_id})
        try:
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    break
                yield sse(*item)
        finally:
            cancel.set()  # client went away -> stop the crawler

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/cancel/{run_id}")
def cancel_run(run_id: str):
    ev = _cancel_events.get(run_id)
    if not ev:
        return {"cancelled": False, "reason": "no active run with that id"}
    ev.set()
    return {"cancelled": True}
