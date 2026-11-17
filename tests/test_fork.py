from __future__ import annotations

import json
import os
import signal
import threading

import pytest

from _support import CHROME, FakeServer, SigningKeys, config_payload, make_client

KEYS = SigningKeys()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_child_resets_threads_locks_and_queue() -> None:
    server = FakeServer(KEYS.envelope(config_payload()))
    lp = make_client(server, flush_interval_ms=60_000)
    lp.preload()
    parent_quote = lp.get_price("pro", {"user-agent": CHROME})
    assert len(lp._queue) == 1

    held = threading.Event()
    release = threading.Event()

    def hold_locks() -> None:
        with lp._lock, lp._queue_lock, lp._drain_lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold_locks)
    holder.start()
    held.wait(5)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            signal.alarm(5)
            fresh = lp._timer is None and lp._inflight is None and lp._queue == []
            quote = lp.get_price("pro", {"user-agent": CHROME})
            lp.flush()
            result = {
                "fresh": fresh,
                "reason": quote.reason,
                "sent": [e["visitorId"] for e in server.events],
                "visitor": quote.visitor_id,
            }
            os.write(write_fd, json.dumps(result).encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    chunks = []
    while True:
        chunk = os.read(read_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    os.waitpid(pid, 0)
    release.set()
    holder.join()

    child = json.loads(b"".join(chunks) or b"{}")
    assert child.get("fresh") is True
    assert child["reason"] == "experiment"
    assert child["sent"] == [child["visitor"]]
    assert child["visitor"] != parent_quote.visitor_id

    lp.flush()
    assert [e["visitorId"] for e in server.events] == [parent_quote.visitor_id]
