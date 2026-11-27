"""Observe OpenRouter usage inside one RAH-launched upstream process.

Usage: python openrouter_observer.py --out <usage.jsonl> -- <target.py> [args...]

The observer wraps httpx/httpx2 Client.send and AsyncClient.send in this process only. It never changes a
request, response bytes, status code, exception or close behaviour, and it records only numeric usage, model,
provider and generation ID for POSTs to OpenRouter's model endpoints. It never imports RAH.
"""
import json
import math
import os
import runpy
import sys
import threading
from urllib.parse import urlsplit

PATHS = frozenset({"/api/v1/chat/completions", "/api/v1/completions", "/api/v1/embeddings", "/api/v1/responses"})
HOST = "openrouter.ai"
BODY_LIMIT = 16 * 1024 * 1024
LINE_LIMIT = 64 * 1024
TEXT_LIMIT = 200


class Log:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, record):
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()


def selected(request):
    try:
        if str(request.method).upper() != "POST":
            return None
        address = urlsplit(str(request.url))
        if (address.hostname or "").lower() != HOST or address.path not in PATHS:
            return None
        return address.path
    except Exception:
        return None


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def text(value):
    return value[:TEXT_LIMIT] if isinstance(value, str) else None


def usage_fields(data):
    """Return the numeric usage fields from an OpenRouter response or chunk, or None."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    prompt = count(usage.get("prompt_tokens"))
    completion = count(usage.get("completion_tokens"))
    if prompt is None and completion is None:  # Responses API spelling.
        prompt, completion = count(usage.get("input_tokens")), count(usage.get("output_tokens"))
    total = count(usage.get("total_tokens"))
    byok = usage.get("is_byok") if isinstance(usage.get("is_byok"), bool) else None
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total,
            "cost": number(usage.get("cost")), "is_byok": byok}


def identity(data, into):
    if not isinstance(data, dict):
        return
    source = data.get("response") if isinstance(data.get("response"), dict) else data
    for key in ("id", "model", "provider"):
        if into.get(key) is None and text(source.get(key)):
            into[key] = text(source.get(key))


class JsonBody:
    """Accumulate a non-SSE body (bounded) and parse it once it ends."""

    def __init__(self):
        self.buffer = bytearray()
        self.too_large = False

    def feed(self, chunk):
        if self.too_large:
            return
        if len(self.buffer) + len(chunk) > BODY_LIMIT:
            self.too_large = True
            self.buffer = bytearray()
            return
        self.buffer.extend(chunk)

    def result(self):
        if self.too_large:
            return {}, None, "too_large"
        try:
            data = json.loads(bytes(self.buffer))
        except (ValueError, UnicodeDecodeError):
            return {}, None, "unparsed"
        found = {}
        identity(data, found)
        usage = usage_fields(data)
        return found, usage, None if usage else "no_usage"


class SseBody:
    """Parse server-sent events from chunks already passed through; keep the last usage-bearing event."""

    def __init__(self):
        self.line = bytearray()
        self.found = {}
        self.usage = None
        self.done = False
        self.overflow = False

    def feed(self, chunk):
        if self.overflow:
            return
        for byte_line in bytes(chunk).splitlines(keepends=True):
            if not byte_line.endswith((b"\n", b"\r")):
                self.line.extend(byte_line)
                if len(self.line) > LINE_LIMIT:
                    self.overflow = True
                    self.line = bytearray()
                    return
                continue
            whole = bytes(self.line) + byte_line
            self.line = bytearray()
            if len(whole) > LINE_LIMIT:
                self.overflow = True
                return
            self.event(whole.strip())

    def event(self, line):
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if payload == b"[DONE]":
            self.done = True
            return
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return
        identity(data, self.found)
        usage = usage_fields(data) or usage_fields(data.get("response") if isinstance(data, dict) else None)
        if usage:
            self.usage = usage
        if isinstance(data, dict) and data.get("type") in ("response.completed", "response.incomplete"):
            self.done = True

    def result(self, closed_early=False):
        if self.line and not self.overflow:
            self.event(bytes(self.line).strip())
            self.line = bytearray()
        if self.overflow:
            return self.found, self.usage, "unparsed"
        if closed_early and not (self.done or self.usage):
            return self.found, self.usage, "aborted"
        return self.found, self.usage, None if self.usage else "no_usage"


def parser_for(response):
    try:
        kind = response.headers.get("content-type", "")
    except Exception:
        kind = ""
    return SseBody() if "text/event-stream" in kind.lower() else JsonBody()


class Recorder:
    def __init__(self, log, endpoint, response, streamed):
        self.log, self.endpoint, self.streamed = log, endpoint, streamed
        self.status = getattr(response, "status_code", None)
        self.parser = parser_for(response)
        self.written = False

    def feed(self, chunk):
        try:
            self.parser.feed(chunk)
        except Exception:
            self.parser = None

    def finish(self, aborted=False):
        if self.written:
            return
        self.written = True
        try:
            if self.parser is None:
                found, usage, reason = {}, None, "unparsed"
            elif isinstance(self.parser, SseBody):
                found, usage, reason = self.parser.result(closed_early=aborted)
            else:
                found, usage, reason = self.parser.result()
                if aborted and reason is not None:
                    reason = "aborted"
            if aborted and reason is None and isinstance(self.parser, SseBody) and not (self.parser.done or self.parser.usage):
                reason = "aborted"
            self.log.write({"endpoint": self.endpoint, "status": self.status, "stream": self.streamed,
                            "id": found.get("id"), "model": found.get("model"), "provider": found.get("provider"),
                            "usage": usage, "reason": reason})
        except Exception:
            try:
                self.log.write({"endpoint": self.endpoint, "status": self.status, "stream": self.streamed,
                                "id": None, "model": None, "provider": None, "usage": None, "reason": "unparsed"})
            except Exception:
                pass


def stream_classes(module):
    """Byte-stream wrappers that subclass the module's own stream types, so httpx type checks still pass."""

    class SyncObserved(module.SyncByteStream):
        def __init__(self, inner, recorder):
            self.inner, self.recorder = inner, recorder

        def __iter__(self):
            try:
                for chunk in self.inner:
                    self.recorder.feed(chunk)
                    yield chunk
            except BaseException:
                self.recorder.finish(aborted=True)
                raise
            self.recorder.finish()

        def close(self):
            try:
                self.inner.close()
            finally:
                self.recorder.finish(aborted=True)

    class AsyncObserved(module.AsyncByteStream):
        def __init__(self, inner, recorder):
            self.inner, self.recorder = inner, recorder

        async def __aiter__(self):
            try:
                async for chunk in self.inner:
                    self.recorder.feed(chunk)
                    yield chunk
            except BaseException:
                self.recorder.finish(aborted=True)
                raise
            self.recorder.finish()

        async def aclose(self):
            try:
                await self.inner.aclose()
            finally:
                self.recorder.finish(aborted=True)

    return SyncObserved, AsyncObserved


def observe(log, module, endpoint, response, sync_class, async_class, is_async):
    """Record a response. A read body is parsed directly; a streamed one is wrapped without reading ahead."""
    try:
        if hasattr(response, "_content"):
            recorder = Recorder(log, endpoint, response, False)
            recorder.feed(response._content)
            recorder.finish()
            return response
        recorder = Recorder(log, endpoint, response, True)
        wrapper = async_class if is_async else sync_class
        response.stream = wrapper(response.stream, recorder)
    except Exception:
        try:
            log.write({"endpoint": endpoint, "status": getattr(response, "status_code", None), "stream": None,
                       "id": None, "model": None, "provider": None, "usage": None, "reason": "unparsed"})
        except Exception:
            pass
    return response


def no_response(log, endpoint):
    try:
        log.write({"endpoint": endpoint, "status": None, "stream": None, "id": None, "model": None,
                   "provider": None, "usage": None, "reason": "no_response"})
    except Exception:
        pass


def install(module, log):
    send, send_async = module.Client.send, module.AsyncClient.send
    sync_class, async_class = stream_classes(module)

    def observed_send(client, request, *args, **kwargs):
        endpoint = selected(request)
        if not endpoint:
            return send(client, request, *args, **kwargs)
        try:
            response = send(client, request, *args, **kwargs)
        except BaseException:
            no_response(log, endpoint)
            raise
        return observe(log, module, endpoint, response, sync_class, async_class, False)

    async def observed_send_async(client, request, *args, **kwargs):
        endpoint = selected(request)
        if not endpoint:
            return await send_async(client, request, *args, **kwargs)
        try:
            response = await send_async(client, request, *args, **kwargs)
        except BaseException:
            no_response(log, endpoint)
            raise
        return observe(log, module, endpoint, response, sync_class, async_class, True)

    module.Client.send, module.AsyncClient.send = observed_send, observed_send_async


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 4 or argv[0] != "--out" or argv[2] != "--":
        sys.stderr.write("usage: openrouter_observer.py --out <usage.jsonl> -- <target.py> [args...]\n")
        raise SystemExit(2)
    log, target, args = Log(argv[1]), argv[3], argv[4:]
    for name in ("httpx", "httpx2"):
        try:
            module = __import__(name)
        except ImportError:
            continue
        try:
            install(module, log)
        except Exception:
            pass
    sys.argv = [target, *args]
    sys.path[0] = os.path.dirname(os.path.abspath(target))
    runpy.run_path(target, run_name="__main__")


if __name__ == "__main__":
    main()
