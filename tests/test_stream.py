from __future__ import annotations

import json
import subprocess
import sys
import threading

import httpx

from papertrade_client.stream import AccountStream

from .conftest import WALLET, account_frame


def _stream(**kwargs) -> AccountStream:
    return AccountStream(WALLET, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))), **kwargs)


def test_stop_waits_for_a_running_listener_and_no_listener_runs_after():
    entered, release, calls = threading.Event(), threading.Event(), []

    def listener(state):
        calls.append(state)
        entered.set()
        release.wait(5)

    stream = _stream(on_change=listener)
    frame = json.dumps(account_frame())
    worker = threading.Thread(target=stream.handle, args=("wallet", frame))
    worker.start()
    assert entered.wait(5)

    stopper = threading.Thread(target=stream.stop)
    stopper.start()
    stopper.join(0.2)
    assert stopper.is_alive()  # still waiting: the listener may be writing state.json
    release.set()
    stopper.join(5)
    worker.join(5)
    assert not stopper.is_alive()

    stream.handle("wallet", frame)
    assert len(calls) == 1
    assert stream.state() is not None  # the state itself keeps updating


SCRIPT = """
import sys, threading, time, httpx
from pathlib import Path
from papertrade_client.store import StateStore
from papertrade_client.stream import AccountStream

data_dir, frame = sys.argv[1], Path(sys.argv[2]).read_text()
store, entered = StateStore(data_dir), threading.Event()

def listener(state):
    def slow(s):
        entered.set()
        time.sleep(0.5)
        s["nonces"]["written"] = "yes"
    store.update(slow)

body = f"event: wallet\\ndata: {frame}\\n\\n".encode()
client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)))
AccountStream("0x" + "11" * 20, client=client, on_change=listener).start()
entered.wait(5)
# The command returns here, mid-write, like `papertrade probe` right after its open.
"""


def test_a_command_that_returns_mid_write_still_lands_the_write(tmp_path):
    (tmp_path / "frame.json").write_text(json.dumps(account_frame()))
    data = tmp_path / "data"
    subprocess.run([sys.executable, "-c", SCRIPT, str(data), str(tmp_path / "frame.json")], check=True, timeout=30)
    assert json.loads((data / "state.json").read_text())["nonces"] == {"written": "yes"}
    assert not list(data.glob(".state.*.tmp"))
