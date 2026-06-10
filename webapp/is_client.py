import json
import logging
from typing import Any

import requests
from flask import current_app

logger = logging.getLogger(__name__)


class ISResponse(dict):
    @property
    def status_code(self) -> int:
        return self["status_code"]

    @property
    def text(self) -> str:
        return str(self.get("raw_text", ""))

    @property
    def content(self) -> bytes:
        return bytes(self.get("raw_content", b""))

    def json(self) -> Any:
        return self["data"]


class ISClient:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def call(self, method: str, path: str, token: str = None, **kwargs) -> ISResponse:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))
        logger.info("IS API call: %s %s", method.upper(), url)
        if "json" in kwargs:
            logger.info("IS API request body: %s", json.dumps(kwargs["json"], default=str)[:500])
        if current_app:
            verify_tls = current_app.config.get("IS_VERIFY_TLS", False)
        else:
            verify_tls = False
        timeout = kwargs.pop("timeout", (3, 10))
        resp = requests.request(method, url, headers=headers, verify=verify_tls, timeout=timeout, **kwargs)
        logger.info("IS API response: %s %s -> %s", method.upper(), path, resp.status_code)
        debug = {
            "method": method.upper(),
            "url": url,
            "request_headers": {
                k: (v[:20] + "..." if k == "Authorization" else v)
                for k, v in headers.items()
            },
            "request_body": kwargs.get("json"),
            "status_code": resp.status_code,
            "response_body": None,
            "curl": self._to_curl(method, url, headers, kwargs.get("json")),
        }
        data = None
        if resp.content:
            try:
                data = resp.json()
                debug["response_body"] = data
            except ValueError:
                data = resp.text
                debug["response_body"] = data
                logger.warning("IS API returned non-JSON response: %s", data[:200])
        return ISResponse({
            "data": data,
            "debug": debug,
            "status_code": resp.status_code,
            "raw_text": resp.text,
            "raw_content": resp.content,
        })

    def get(self, path: str, **kwargs) -> ISResponse:
        return self.call("get", path, **kwargs)

    def post(self, path: str, **kwargs) -> ISResponse:
        return self.call("post", path, **kwargs)

    def put(self, path: str, **kwargs) -> ISResponse:
        return self.call("put", path, **kwargs)

    def patch(self, path: str, **kwargs) -> ISResponse:
        return self.call("patch", path, **kwargs)

    def delete(self, path: str, **kwargs) -> ISResponse:
        return self.call("delete", path, **kwargs)

    def _to_curl(self, method, url, headers, body):
        parts = [f"curl -X {method.upper()} '{url}'"]
        for k, v in headers.items():
            display_v = v[:20] + "..." if k == "Authorization" else v
            parts.append(f"-H '{k}: {display_v}'")
        if body:
            parts.append(f"-d '{json.dumps(body, indent=2)}'")
        return " \\\n  ".join(parts)
