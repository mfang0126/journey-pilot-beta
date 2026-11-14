#!/usr/bin/env python3
"""Serve one short-lived local page that puts exact text on the phone clipboard.

Text arrives on stdin and stays in this process's memory only. The URL carries
a random token; no access log is written. The phone opens the URL in Chrome and
taps COPY; every phone action still goes through journey-pilot. The page uses
execCommand('copy') on a user gesture, so it works on a plain LAN HTTP origin
where navigator.clipboard is unavailable.

This is a host helper, not a phone action and not a general web server.
"""
from __future__ import annotations

import argparse
import html
import http.server
import json
import secrets
import socket
import sys
import threading


def lan_ip() -> str:
    """The address the default route would use; no packet is sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def page(text: str) -> bytes:
    literal = json.dumps(text).replace("</", "<\\/")
    body = f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>journey-pilot clipboard</title>
<style>
body{{font:20px/1.4 system-ui;margin:24px}}
textarea{{width:100%;height:35vh;font:inherit;box-sizing:border-box}}
button{{font:inherit;padding:16px 28px;margin-top:16px;width:100%}}
</style>
<textarea id="text" readonly>{html.escape(text)}</textarea>
<button id="copy" onclick="copyText()">COPY</button>
<script>
const value = {literal};
function done( ok ) {{
  const b = document.getElementById('copy');
  b.textContent = ok ? 'COPIED' : 'COPY FAILED';
  b.disabled = ok;
}}
function copyText() {{
  const area = document.getElementById('text');
  area.focus(); area.select();
  let ok = false;
  try {{ ok = document.execCommand('copy'); }} catch (e) {{ ok = false; }}
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(value).then(() => done(true), () => done(ok));
  }} else {{
    done(ok);
  }}
}}
</script>
"""
    return body.encode("utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    text = ""
    route = "/"

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path != type(self).route:
            self.send_error(404)
            return
        body = page(type(self).text)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the secret path and text out of logs
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="bind address (default: this machine's LAN address)")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ttl", type=float, default=600.0, help="seconds before self-shutdown")
    args = parser.parse_args()

    text = sys.stdin.read()
    if not text.strip():
        print("clipboard-page: expected text on stdin", file=sys.stderr)
        return 2
    Handler.text = text
    Handler.route = "/" + secrets.token_urlsafe(16)
    host = args.host or lan_ip()

    server = http.server.ThreadingHTTPServer((host, args.port), Handler)
    port = server.server_address[1]
    url = f"http://{host}:{port}{Handler.route}"
    print(url, flush=True)
    timer = threading.Timer(args.ttl, server.shutdown)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        Handler.text = ""
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
