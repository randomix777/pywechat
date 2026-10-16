from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class CloudError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudResponse:
    status_code: int
    data: dict[str, Any]


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


class CloudClient:
    def __init__(self, base_url: str, device_token: str, timeout: float = 10, agent_id: str = "default", allow_insecure_remote: bool = False):
        if not base_url:
            raise ValueError("base_url is required")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an HTTP(S) URL")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and parsed.hostname not in local_hosts and not allow_insecure_remote:
            raise ValueError("HTTPS is required for remote cloud endpoints")
        self.base_url = base_url.rstrip("/")
        self.device_token = device_token
        self.timeout = timeout
        self.agent_id = agent_id
        self.allow_insecure_remote = allow_insecure_remote
        self._opener = build_opener(_NoRedirectHandler())

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> CloudResponse:
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "wechat-ai-assistant-agent/1",
            "X-Agent-Id": self.agent_id,
        }
        if self.device_token:
            headers["Authorization"] = f"Bearer {self.device_token}"
        if payload is not None:
            data = json.dumps(dict(payload), ensure_ascii=False, default=str).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw_body = response.read(2 * 1024 * 1024 + 1)
                if len(raw_body) > 2 * 1024 * 1024:
                    raise CloudError("cloud response is too large")
                body = raw_body.decode("utf-8") if raw_body else ""
                result = json.loads(body) if body else {}
                if not isinstance(result, dict):
                    result = {"data": result}
                return CloudResponse(status_code=response.status, data=result)
        except HTTPError as exc:
            try:
                raw_error = exc.read(2 * 1024 * 1024 + 1)
                if len(raw_error) > 2 * 1024 * 1024:
                    raise CloudError("cloud error response is too large") from exc
                body = raw_error.decode("utf-8", errors="replace")
                raise CloudError(f"cloud HTTP {exc.code}: {body[:500]}") from exc
            finally:
                exc.close()
        except (URLError, TimeoutError, OSError) as exc:
            raise CloudError(f"cloud request failed: {exc}") from exc

    def heartbeat(self, health: Mapping[str, Any]) -> CloudResponse:
        return self._request("POST", "/api/v1/agent/heartbeat", {"health": dict(health)})

    def send_events(self, events: list[Mapping[str, Any]]) -> CloudResponse:
        return self._request("POST", "/api/v1/agent/events", {"events": [dict(event) for event in events]})

    def send_business_events(self, events: list[Mapping[str, Any]]) -> CloudResponse:
        """Upload cached collection events into the tenant-bound business inbox."""
        response=self._request("POST", "/api/v1/agent/business-events", {"events": [dict(event) for event in events]})
        if int(response.data.get("rejected",0)):
            raise CloudError(f"business ingress rejected {response.data['rejected']} event(s); check device group authorization")
        return response

    def fetch_commands(self, limit: int = 20) -> list[dict[str, Any]]:
        response = self._request("GET", f"/api/v1/agent/commands?limit={max(1, min(limit, 100))}")
        commands = response.data.get("commands", [])
        return [dict(command) for command in commands if isinstance(command, Mapping)]

    def acknowledge(self, command_id: str, result: Mapping[str, Any]) -> CloudResponse:
        path = f"/api/v1/agent/commands/{quote(command_id, safe='')}/ack"
        response = self._request("POST", path, {"result": dict(result)})
        if response.data.get("updated") is not True:
            raise CloudError(f"cloud rejected command acknowledgement: {response.data.get('error', 'command was not updated')}")
        return response
