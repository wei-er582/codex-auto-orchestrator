"""Print only model/service-tier evidence from a mitmproxy WebSocket capture.

This optional release-validation helper deliberately omits headers, prompts, and
response text so a real Codex smoke test can be audited without exposing auth
material or task content.
"""

from __future__ import annotations

import json
from typing import Any

from mitmproxy import http


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class ServiceTierEvidence:
    def websocket_message(self, flow: http.HTTPFlow) -> None:
        websocket = flow.websocket
        if websocket is None or not websocket.messages:
            return
        message = websocket.messages[-1]
        if not message.is_text:
            return
        try:
            payload = json.loads(message.text)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return

        event_type = _string(payload.get("type"))
        record: dict[str, Any] = {
            "direction": "client" if message.from_client else "server",
            "host": flow.request.pretty_host,
            "path": flow.request.path,
            "event_type": event_type,
        }
        if event_type == "response.create" and message.from_client:
            record.update(
                {
                    "model": _string(payload.get("model")),
                    "requested_service_tier": _string(payload.get("service_tier")),
                }
            )
        elif event_type == "response.completed" and not message.from_client:
            response = payload.get("response")
            if not isinstance(response, dict):
                return
            record.update(
                {
                    "response_id": _string(response.get("id")),
                    "observed_service_tier": _string(response.get("service_tier")),
                    "model": _string(response.get("model")),
                }
            )
        else:
            return
        print(json.dumps(record, ensure_ascii=False, sort_keys=True), flush=True)


addons = [ServiceTierEvidence()]
