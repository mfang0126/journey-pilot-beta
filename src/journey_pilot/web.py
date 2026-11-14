"""Web locators: find and tap DOM elements in the foreground Chrome tab over CDP.

Chrome exposes a page as one accessibility node, so web content is reached
through ``adb forward tcp:<port> localabstract:chrome_devtools_remote``.  A web
locator uses ``css``, ``web_text`` (exact visible text) and/or
``web_text_contains``; it never mixes with the native keys.  The forward is
created per call and removed again, only the port this call allocated.

Only the visible tab is addressed.  Elements must have a layout box; a tap is a
real touch event (``Input.dispatchTouchEvent``), not ``element.click()``.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import urllib.request
from typing import Any, Mapping

WEB_KEYS = ("css", "web_text", "web_text_contains")
_SOCKET = "chrome_devtools_remote"
_FIND = """((q) => {
  const shown = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const text = (e) => (e.innerText || e.value || '').trim();
  let els = [...document.querySelectorAll(q.css || 'body *')].filter(shown);
  if (q.web_text !== undefined) els = els.filter((e) => text(e) === q.web_text);
  if (q.web_text_contains !== undefined) els = els.filter((e) => text(e).includes(q.web_text_contains));
  if (!q.css) els = els.filter((e) => !els.some((o) => o !== e && e.contains(o)));
  return els.map((e) => { const r = e.getBoundingClientRect();
    return {text: text(e).slice(0, 200), x: r.left + r.width / 2, y: r.top + r.height / 2}; });
})(%s)"""


class WebUnavailable(Exception):
    """No usable Chrome tab or DevTools socket; nothing was touched."""


def is_web_locator(loc: Any) -> bool:
    return isinstance(loc, Mapping) and any(key in loc for key in WEB_KEYS)


def has_web(spec: Any) -> bool:
    """True if a check (or a nested check) uses a web locator."""
    if not isinstance(spec, Mapping):
        return False
    for key in ("all", "any"):
        if key in spec:
            return any(has_web(sub) for sub in spec[key])
    if "not" in spec:
        return has_web(spec["not"])
    value = spec.get("element_exists", spec.get("element_count", spec.get("text_equals")))
    return is_web_locator(value) or (isinstance(value, Mapping) and "css" in value)


def text_equals_locator(value: Mapping[str, Any]) -> dict[str, str]:
    """``text_equals`` object form: the element at ``css`` shows exactly ``text``."""
    return {"css": value["css"], "web_text": value["text"]}


class _Socket:
    """Just enough RFC 6455 for one JSON request/response conversation."""

    def __init__(self, port: int, path: str, timeout: float):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                           f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise WebUnavailable("DevTools closed the handshake")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise WebUnavailable("DevTools refused the websocket")
        self.buffer = head.split(b"\r\n\r\n", 1)[1]

    def _read(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebUnavailable("DevTools closed the connection")
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def send(self, text: str) -> None:
        payload = text.encode()
        n = len(payload)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else
                                bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                                bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self) -> str:
        message = b""
        while True:
            first, second = self._read(2)
            n = second & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if first & 0x0F == 8:
                raise WebUnavailable("DevTools closed the connection")
            if first & 0x0F in (1, 0):  # text / continuation; ping and pong are skipped
                message += payload
                if first & 0x80:
                    return message.decode()

    def close(self) -> None:
        self.sock.close()


class Chrome:
    """Context manager: forward the DevTools socket and attach to the visible tab."""

    def __init__(self, serial: str | None = None, timeout: float = 10.0):
        self.adb = os.environ.get("ADB_BIN") or "adb"
        self.base = [self.adb, *(["-s", serial] if serial else [])]
        self.timeout = timeout
        self.port: int | None = None
        self.ws: _Socket | None = None
        self._id = 0

    def __enter__(self) -> "Chrome":
        try:
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
            probe.close()
            out = subprocess.run([*self.base, "forward", f"tcp:{self.port}", f"localabstract:{_SOCKET}"],
                                 capture_output=True, text=True, timeout=self.timeout)
            if out.returncode != 0:
                self.port = None
                raise WebUnavailable((out.stderr or out.stdout).strip()[:160] or "adb forward failed")
            self._attach()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.ws:
            self.ws.close()
        if self.port is not None:
            subprocess.run([*self.base, "forward", "--remove", f"tcp:{self.port}"],
                           capture_output=True, timeout=self.timeout)

    def _attach(self) -> None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=self.timeout) as resp:
                targets = json.load(resp)
        except (OSError, ValueError) as exc:
            raise WebUnavailable(f"Chrome DevTools unreachable: {exc}") from exc
        for target in targets:
            if target.get("type") != "page" or not target.get("webSocketDebuggerUrl"):
                continue
            path = "/" + target["webSocketDebuggerUrl"].split("/", 3)[3]
            ws = _Socket(self.port, path, self.timeout)
            self.ws = ws
            if self.call("Runtime.evaluate", expression="document.visibilityState",
                         returnByValue=True)["result"]["value"] == "visible":
                return
            ws.close()
            self.ws = None
        raise WebUnavailable("no visible Chrome tab")

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            reply = json.loads(self.ws.recv())
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise WebUnavailable(str(reply["error"])[:160])
                return reply["result"]

    def find(self, locator: Mapping[str, str]) -> list[dict[str, Any]]:
        result = self.call("Runtime.evaluate", returnByValue=True,
                           expression=_FIND % json.dumps(dict(locator)))
        return result["result"].get("value") or []

    def page_text(self) -> str:
        result = self.call("Runtime.evaluate", returnByValue=True,
                           expression="document.body ? document.body.innerText : ''")
        return result["result"].get("value") or ""

    def tap(self, x: float, y: float) -> None:
        for kind, points in (("touchStart", [{"x": x, "y": y}]), ("touchEnd", [])):
            self.call("Input.dispatchTouchEvent", type=kind, touchPoints=points)


def count(locator: Mapping[str, str], serial: str | None = None) -> int:
    """Visible elements matching ``locator`` in the foreground tab.

    Raises ``WebUnavailable`` when the page cannot be queried: a missing tab is
    not "zero matches", or ``not`` / ``element_count 0`` checks would pass blind.
    """
    with Chrome(serial) as chrome:
        return len(chrome.find(locator))


def page_text(serial: str | None = None) -> str:
    """Visible text of the foreground tab, '' when it cannot be read."""
    try:
        with Chrome(serial) as chrome:
            return chrome.page_text()
    except WebUnavailable:
        return ""
