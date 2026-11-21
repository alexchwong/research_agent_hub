from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from threading import Lock
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .common import Problem, digest


FIELDS = ("usage", "usage_daily", "usage_weekly", "usage_monthly",
          "byok_usage", "byok_usage_daily", "byok_usage_weekly", "byok_usage_monthly")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def request_json(path, key):
    request = Request("https://openrouter.ai/api/v1/" + path, headers={"Accept": "application/json",
                      **({"Authorization": "Bearer " + key} if key else {})})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
            body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError
        return json.loads(body, parse_float=Decimal)["data"]
    except HTTPError as error:
        if error.code in (401, 403):
            raise Problem("authorization_failed", "OpenRouter rejected the shared key.", 503) from error
        if error.code == 429:
            raise Problem("rate_limited", "OpenRouter rate limited the request.", 503) from error
        raise Problem("provider_error", "OpenRouter is unavailable.", 503) from error
    except (URLError, OSError) as error:
        raise Problem("provider_error", "OpenRouter did not respond.", 503) from error
    except (ValueError, KeyError, TypeError) as error:
        raise Problem("invalid_response", "OpenRouter returned an invalid response.", 503) from error


def amount(value):
    if not isinstance(value, (Decimal, int)) or isinstance(value, bool):
        return None
    try:
        number = Decimal(value)
        if number.is_finite() and number >= 0 and abs(number.adjusted()) <= 100:
            return format(number, "f")
    except InvalidOperation:
        pass
    return None


class OpenRouter:
    def __init__(self, get_key):
        self.get_key = get_key
        self.lock = Lock()
        self.snapshot = None
        self.catalogue = {}
        self.error = None
        self.associated_key = None

    def sync_key(self, key):
        fingerprint = digest(key.encode()) if key else None
        if self.associated_key != fingerprint:
            self.snapshot = self.error = None
            self.catalogue = {}
            self.associated_key = fingerprint
        return fingerprint

    def usage(self):
        key = self.get_key()
        with self.lock:
            self.sync_key(key)
            data = self.snapshot
            return {"fields": data[0] if data else dict.fromkeys(FIELDS), "refreshed_at": data[1] if data else None,
                    "stale": bool(data and (self.error or monotonic() - data[2] > 900)),
                    "error": self.error or ("missing_key" if not key else None), "currency": "USD"}

    def refresh(self):
        key = self.get_key()
        with self.lock:
            fingerprint = self.sync_key(key)
        if not key:
            return self.usage()
        try:
            response = request_json("key", key)
            if not isinstance(response, dict):
                raise Problem("invalid_response", "OpenRouter returned an invalid usage response.", 503)
            values = {field: amount(response.get(field)) for field in FIELDS}
        except Problem as error:
            with self.lock:
                if self.sync_key(self.get_key()) == fingerprint:
                    self.error = error.code
        else:
            with self.lock:
                if self.sync_key(self.get_key()) == fingerprint:
                    self.snapshot = (values, datetime.now(timezone.utc).isoformat(), monotonic())
                    self.error = None
        return self.usage()

    def models(self, family="chat"):
        key = self.get_key()
        with self.lock:
            fingerprint = self.sync_key(key)
            cached = self.catalogue.get(family)
            if cached and monotonic() - cached[1] < 900:
                return cached[0]
        response = request_json("embeddings/models" if family == "embedding" else "models", key)
        if not isinstance(response, list):
            raise Problem("invalid_response", "OpenRouter returned an invalid model catalogue.", 503)
        models = []
        for item in response[:10000]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            arch = item.get("architecture") or {}
            if family == "embedding" or (isinstance(arch, dict) and "text" in (arch.get("output_modalities") or [])):
                models.append(item["id"])
        with self.lock:
            if self.sync_key(self.get_key()) == fingerprint:
                self.catalogue[family] = (models, monotonic())
        return models
