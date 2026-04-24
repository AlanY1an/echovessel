#!/usr/bin/env python3
"""Drive one chat turn end-to-end: send, collect tokens, print reply.

Usage: ``python scripts/dogfood_drive.py "<user message>"``

Opens the SSE stream, POSTs one chat/send, collects every
``chat.message.token`` delta until ``chat.message.done``, then prints
the assembled reply. Exits non-zero if no ``done`` within 60s.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from queue import Empty, Queue

import httpx

BASE = "http://localhost:7777"


def _sse_reader(q: Queue, stop: threading.Event) -> None:
    with httpx.Client(timeout=None) as client:
        with client.stream("GET", f"{BASE}/api/chat/events") as resp:
            event_type: str | None = None
            for line in resp.iter_lines():
                if stop.is_set():
                    return
                if not line:
                    event_type = None
                    continue
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    payload = line[5:].strip()
                    if event_type and payload:
                        try:
                            q.put((event_type, json.loads(payload)))
                        except json.JSONDecodeError:
                            pass


def drive_one(content: str, user_id: str = "self", timeout_s: int = 60) -> str:
    q: Queue = Queue()
    stop = threading.Event()
    t = threading.Thread(target=_sse_reader, args=(q, stop), daemon=True)
    t.start()

    # Wait for connection.ready
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            evt, _ = q.get(timeout=0.3)
            if evt == "chat.connection.ready":
                break
        except Empty:
            continue

    # Send
    resp = httpx.post(
        f"{BASE}/api/chat/send",
        json={"content": content, "user_id": user_id},
        timeout=10,
    )
    resp.raise_for_status()
    turn_id: str | None = None

    tokens: list[str] = []
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            evt, data = q.get(timeout=1.0)
        except Empty:
            continue
        if evt == "chat.message.user_appended":
            turn_id = data.get("turn_id") or turn_id
        elif evt == "chat.message.token":
            if turn_id is None or data.get("turn_id") == turn_id:
                tokens.append(data.get("delta", ""))
        elif evt == "chat.message.done":
            if turn_id is None or data.get("turn_id") == turn_id:
                stop.set()
                final = data.get("content") or "".join(tokens)
                return final
    stop.set()
    raise TimeoutError(f"no chat.message.done within {timeout_s}s")


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: dogfood_drive.py '<message>' [user_id]", file=sys.stderr)
        sys.exit(2)
    message = sys.argv[1]
    user_id = sys.argv[2] if len(sys.argv) > 2 else "self"
    reply = drive_one(message, user_id=user_id)
    print("=" * 60)
    print(f"USER  : {message}")
    print("-" * 60)
    print(f"REPLY : {reply}")
    print("=" * 60)


if __name__ == "__main__":
    main()
