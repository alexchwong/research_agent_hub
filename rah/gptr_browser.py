"""Run the pinned upstream Classic UI on one guarded loopback listener."""
import hmac
import json
import os
import re
import sys
from pathlib import Path


async def report_source_status(app, scope, receive, send):
    """Annotate empty native web reports without changing research or exports."""
    web_only = missing = found = False

    async def observe_request():
        nonlocal web_only, missing, found
        message = await receive()
        text = message.get("text") or ""
        if text.startswith("start "):
            web_only = missing = found = False
            try:
                options = json.loads(text[6:])
                web_only = (isinstance(options, dict) and options.get("report_source", "web") == "web"
                            and not any(options.get(key) for key in ("source_urls", "document_urls", "mcp_enabled", "mcp_configs")))
            except (ValueError, RecursionError):
                pass
        return message

    async def observe_output(message):
        nonlocal missing, found
        if web_only and message["type"] == "websocket.send" and message.get("text"):
            try:
                data = json.loads(message["text"])
            except (ValueError, RecursionError):
                data = None
            if isinstance(data, dict):
                if data.get("type") == "logs":
                    event = data.get("content")
                    if event == "subquery_context_not_found":
                        missing = True
                    elif event == "context_combined":
                        found = True
                    elif event == "scraping_content" and isinstance(data.get("output"), str):
                        match = re.fullmatch(r"📄 Scraped (\d+) pages of content", data["output"])
                        if match:
                            missing |= match[1] == "0"
                            found |= match[1] != "0"
                if data.get("type") == "path" and missing and not found:
                    await send({"type": "websocket.send", "text": json.dumps({"type": "logs", "content": "rah_no_sources",
                                "output": "No sources retrieved. Treat this output as unsourced; check the search source or try a different query."})})
        await send(message)

    await app(scope, observe_request, observe_output)


class LocalBrowser:
    def __init__(self, app, port, startup_token, stop=None, *, guard_native=True):
        self.app = app
        self.host = f"127.0.0.1:{port}".encode()
        self.origin = b"http://" + self.host
        self.startup_token = startup_token.encode()
        self.stop = stop
        self.guard_native = guard_native

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        control = kind == "http" and scope["path"] in {"/_rah_identity", "/_rah_stop"}
        if kind == "lifespan" or not (control or self.guard_native):
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        hosts = [value for key, value in headers if key == b"host"]
        origins = [value for key, value in headers if key == b"origin"]
        sites = [value for key, value in headers if key == b"sec-fetch-site"]
        method = scope.get("method")
        safe = kind == "http" and method in {"GET", "HEAD"}
        local = hosts == [self.host] and (origins == [self.origin] or safe and not origins)
        direct_navigation = (safe and scope["path"] == "/" and not origins
                             and (b"sec-fetch-mode", b"navigate") in headers
                             and (b"sec-fetch-dest", b"document") in headers)
        foreign = any(site not in {b"none", b"same-origin"} for site in sites)
        if not local or method == "OPTIONS" or (foreign and not direct_navigation):
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 403, "headers": []})
                await send({"type": "http.response.body", "body": b"Use the native loopback URL."})
            return
        if control:
            def header(name):
                values = [value for key, value in headers if key == name]
                return values[0] if len(values) == 1 else b""

            nonce = header(b"x-rah-nonce")
            status, body = 403, b""
            if self.startup_token and re.fullmatch(rb"[0-9a-f]{64}", nonce):
                if method == "GET" and scope["path"] == "/_rah_identity":
                    status, body = 200, hmac.new(self.startup_token, b"identity:" + nonce, "sha256").hexdigest().encode()
                elif (method == "POST" and scope["path"] == "/_rah_stop" and self.stop and origins == [self.origin]
                      and hmac.compare_digest(header(b"x-rah-proof"),
                                              hmac.new(self.startup_token, b"stop:" + nonce, "sha256").hexdigest().encode())):
                    status = 204
            await send({"type": "http.response.start", "status": status, "headers": [(b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})
            if status == 204:
                self.stop()
            return

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                ]
            await send(message)

        if kind == "websocket" and scope["path"] == "/ws":
            await report_source_status(self.app, scope, receive, secure_send)
        else:
            await self.app(scope, receive, secure_send)


def main():
    source, port = Path(sys.argv[1]).resolve(strict=True), int(sys.argv[2])
    sys.path.insert(0, str(source))
    os.environ["CORS_ALLOW_ORIGINS"] = f"http://127.0.0.1:{port}"
    token = os.environ.pop("RAH_GPTR_STARTUP_TOKEN", "")
    hub = os.environ.pop("RAH_HANDOFF_HUB", "")
    from main import app
    import uvicorn
    if token and hub:
        from native_handoff import NativeHandoff
        app = NativeHandoff(app, "gptr", port, token, hub)
    def stop():
        server.should_exit = True

    server = uvicorn.Server(uvicorn.Config(LocalBrowser(app, port, token, stop), host="127.0.0.1", port=port,
                                          proxy_headers=False, access_log=False))
    server.run()


if __name__ == "__main__":
    main()
