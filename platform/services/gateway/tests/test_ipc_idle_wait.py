"""Local IPC loop wait: with no connection open the loop waits IDLE_WAIT_S for
accept/stop instead of polling every POLL_WAIT_S, and close() still returns well
inside its 0.5 s join.

The listener is a plain TCP socket so the loop itself runs on any platform; no
connection is made, so peer-credential checks are not reached.
"""
import selectors
import socket
import threading
import time

from xenova_gateway import ipc
from xenova_gateway.ipc import IDLE_WAIT_S, POLL_WAIT_S, GatewayServer


def test_idle_loop_waits_for_accept_or_stop_and_stops_promptly(monkeypatch):
    waits = []

    class Recording(selectors.DefaultSelector):
        def select(self, timeout=None):
            waits.append(timeout)
            return super().select(timeout)

    monkeypatch.setattr(ipc.selectors, 'DefaultSelector', Recording)
    server = GatewayServer(object(), object(), '/run/xenova-gateway/test.sock', submit=lambda call: None)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.setblocking(False)
    server.listener = listener
    server.thread = threading.Thread(target=server._loop, daemon=True)
    server.thread.start()
    time.sleep(.35)
    stopped = time.monotonic()
    server.stop.set()
    server.thread.join(.5)
    assert not server.thread.is_alive() and server.failure is None
    assert time.monotonic() - stopped < IDLE_WAIT_S + .2
    assert waits and set(waits) == {IDLE_WAIT_S}
    assert len(waits) <= .35 / IDLE_WAIT_S + 2  # POLL_WAIT_S would have been about 35
    assert POLL_WAIT_S < IDLE_WAIT_S < .5
