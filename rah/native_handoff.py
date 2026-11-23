"""Managed native browser adapter. The upstream owns its forms and research."""
import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import re
from time import monotonic
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class NativeHandoff:
    limit = 32 * 1024 * 1024
    script = b'<script src="/_rah_handoff.js" defer></script>'

    def __init__(self, app, kind, port, identity, hub):
        self.app, self.kind, self.identity = app, kind, identity.encode()
        self.host = f"127.0.0.1:{port}".encode()
        self.origin = b"http://" + self.host
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", hub):
            raise ValueError("Invalid RAH handoff origin")
        self.hub = hub
        self.drafts = {}
        self.exports = {}

    async def reply(self, send, status, body=b"", content=b"application/json"):
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", content), (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"), (b"cross-origin-resource-policy", b"same-origin")]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def read(receive, limit):
        buf = bytearray()
        while True:
            part = await receive()
            if part["type"] != "http.request":
                raise ValueError
            buf.extend(part.get("body", b""))
            if len(buf) > limit:
                raise ValueError
            if not part.get("more_body"):
                return bytes(buf)

    def save_export(self, grant, fmt, payload):
        request = Request(self.hub + "/_rah_native_export", data=payload, headers={
            "X-RAH-Export": grant, "X-RAH-Format": fmt, "Content-Type": "application/octet-stream"})
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=30) as response:
                return response.status == 201
        except (OSError, HTTPError):
            return False

    async def action(self, path, scope, receive, send):
        def header(key):
            values = [value for name, value in scope["headers"] if name == key]
            return values[0] if len(values) == 1 else b""

        if header(b"host") != self.host:
            return await self.reply(send, 403)
        method = scope["method"]
        if path == "/_rah_handoff.js" and method == "GET" and header(b"sec-fetch-site") in (b"same-origin", b""):
            script = Path(__file__).with_name("native_handoff.js")
            if not script.is_file():
                script = script.parent / "static" / script.name
            return await self.reply(send, 200, script.read_bytes(), b"text/javascript")
        if method != "POST":
            return await self.reply(send, 405)
        now = monotonic()
        self.drafts = {key: value for key, value in self.drafts.items() if value[0] > now}
        self.exports = {key: value for key, value in self.exports.items() if value[0] > now}
        try:
            if path == "/_rah_handoff_stage":
                raw = await self.read(receive, 64 * 1024)
                proof = hmac.new(self.identity, b"handoff:" + hashlib.sha256(raw).hexdigest().encode(), "sha256").hexdigest()
                if not hmac.compare_digest(header(b"x-rah-proof"), proof.encode()):
                    return await self.reply(send, 403)
                obj = json.loads(raw)
                ticket, question, grant = obj["ticket"], obj["question"], obj.get("grant")
                if not re.fullmatch("[0-9a-f]{64}", ticket) or not isinstance(question, str) or len(question) > 8000:
                    raise ValueError
                if grant and (self.kind != "ldr" or not re.fullmatch("[0-9a-f]{64}", grant)):
                    raise ValueError
                if len(self.drafts) >= 128:
                    return await self.reply(send, 429)
                self.drafts[ticket] = (now + 600, question, grant)
                return await self.reply(send, 204)
            if header(b"origin") != self.origin or header(b"sec-fetch-site") not in (b"", b"same-origin"):
                return await self.reply(send, 403)
            ticket = header(b"x-rah-ticket").decode("ascii")
            if path == "/_rah_handoff_take":
                item = self.drafts.pop(ticket, None)
                if not item:
                    return await self.reply(send, 404)
                if item[2]:
                    if len(self.exports) >= 128:
                        self.exports.pop(next(iter(self.exports)))
                    self.exports[ticket] = (now + 8 * 3600, item[2])
                return await self.reply(send, 200, json.dumps({"question": item[1], "export": bool(item[2])}).encode())
            if path == "/_rah_handoff_export":
                item = self.exports.get(ticket)
                fmt = header(b"x-rah-format").decode("ascii")
                if not item or fmt not in ("md", "ris"):
                    return await self.reply(send, 404)
                raw = await self.read(receive, self.limit)
                saved = await asyncio.to_thread(self.save_export, item[1], fmt, raw)
                return await self.reply(send, 204 if saved else 409)
        except (KeyError, TypeError, ValueError):
            return await self.reply(send, 400)
        return await self.reply(send, 404)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path.startswith("/_rah_handoff"):
            return await self.action(path, scope, receive, send)
        can_inject = scope["method"] == "GET" and (path == "/" or self.kind == "ldr" and (
            path in {"/research", "/research/", "/auth/login", "/auth/register", "/history"}
            or path.startswith(("/results/", "/details/", "/research/results/", "/research/details/"))))
        if not can_inject:
            return await self.app(scope, receive, send)
        start, chunks = None, []

        async def decorate(message):
            nonlocal start
            if message["type"] == "http.response.start":
                headers = dict(message["headers"])
                if message["status"] == 200 and headers.get(b"content-type", b"").startswith(b"text/html") and b"content-encoding" not in headers:
                    start = message
                    return
            if start and message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    body = b"".join(chunks)
                    if b"</body>" in body:
                        body = body.replace(b"</body>", self.script + b"</body>", 1)
                    start["headers"] = [(k, v) for k, v in start["headers"] if k not in (b"content-length", b"etag", b"referrer-policy")]
                    start["headers"].extend([(b"content-length", str(len(body)).encode()), (b"referrer-policy", b"no-referrer")])
                    await send(start)
                    await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        # Inspect uncompressed HTML without changing upstream compression on other routes.
        plain = scope | {"headers": [(k, v) for k, v in scope["headers"] if k != b"accept-encoding"]}
        await self.app(plain, receive, decorate)