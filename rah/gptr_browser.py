"""Run the pinned upstream Classic UI on one guarded loopback listener."""
import os
from pathlib import Path
import sys


class LocalBrowser:
    def __init__(self, app, port, startup_token):
        self.app = app
        self.host = f"127.0.0.1:{port}".encode()
        self.origin = b"http://" + self.host
        self.startup_token = startup_token.encode()

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
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
        if safe and scope["path"] == "/_rah_ready":
            from hmac import compare_digest
            tokens = [value for key, value in headers if key == b"x-rah-native"]
            valid = len(tokens) == 1 and bool(self.startup_token) and compare_digest(tokens[0], self.startup_token)
            await send({"type": "http.response.start", "status": 204 if valid else 403,
                        "headers": [(b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b""})
            return

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                ]
            await send(message)

        await self.app(scope, receive, secure_send)


def main():
    source, port = Path(sys.argv[1]).resolve(strict=True), int(sys.argv[2])
    sys.path.insert(0, str(source))
    os.environ["CORS_ALLOW_ORIGINS"] = f"http://127.0.0.1:{port}"
    token = os.environ.pop("RAH_GPTR_STARTUP_TOKEN", "")
    from main import app
    import uvicorn
    uvicorn.run(LocalBrowser(app, port, token), host="127.0.0.1", port=port,
                proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
