"""Exercise the deployed WebSocket stack, not only Starlette's in-memory client."""

import socket
import threading
import time

import uvicorn

from fast_tabber.server import create_app


def test_real_server_accepts_chrome_websocket_upgrade():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    origin = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
    server = uvicorn.Server(uvicorn.Config(create_app("a" * 64, origin), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Uvicorn did not start"
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            connection.sendall((
                f"GET /extension HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Origin: {origin}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode())
            response = connection.recv(4096)
            assert response.startswith(b"HTTP/1.1 101"), response.split(b"\r\n")[0]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
