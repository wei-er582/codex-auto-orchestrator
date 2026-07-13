from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .util import atomic_write_json, exclusive_file_lock, load_json, utc_now


CONTROL_KINDS = {"cancel", "pause", "resume", "steer", "speed-change"}
PRIORITY = {"cancel": 0, "pause": 1, "steer": 2, "speed-change": 2, "resume": 3}


class ControlQueue:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def read(self) -> dict[str, Any]:
        with exclusive_file_lock(self.lock_path):
            if not self.path.is_file():
                return {"version": 1, "next_seq": 1, "requests": []}
            return _validate(load_json(self.path))

    def enqueue(
        self,
        kind: str,
        *,
        payload: dict[str, Any] | None = None,
        source_thread_id: str = "",
        boundary: str = "safe",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in CONTROL_KINDS:
            raise ValueError(f"unsupported control kind: {kind}")
        if boundary not in {"safe", "immediate"}:
            raise ValueError("control boundary must be safe or immediate")
        with exclusive_file_lock(self.lock_path):
            value = _validate(load_json(self.path)) if self.path.is_file() else {
                "version": 1,
                "next_seq": 1,
                "requests": [],
            }
            sequence = int(value["next_seq"])
            value["next_seq"] = sequence + 1
            request = {
                "seq": sequence,
                "request_id": request_id or str(uuid.uuid4()),
                "kind": kind,
                "payload": payload or {},
                "source_thread_id": source_thread_id,
                "boundary": boundary,
                "status": "pending",
                "created_at": utc_now(),
                "applied_at": "",
                "result": "",
            }
            if kind == "steer" and request["payload"].get("mode") == "replace":
                for existing in value["requests"]:
                    if existing["kind"] == "steer" and existing["status"] == "pending":
                        existing["status"] = "superseded"
                        existing["applied_at"] = utc_now()
                        existing["result"] = f"superseded by request {request['request_id']}"
            value["requests"].append(request)
            atomic_write_json(self.path, value)
            return request

    def pending(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        value = self.read()
        pending = [
            item
            for item in value["requests"]
            if item["status"] == "pending" and int(item["seq"]) > int(after_sequence)
        ]
        return sorted(pending, key=lambda item: (PRIORITY[item["kind"]], int(item["seq"])))

    def complete(self, request_id: str, status: str, result: str) -> dict[str, Any]:
        if status not in {"applied", "rejected", "superseded"}:
            raise ValueError(f"invalid control result status: {status}")
        with exclusive_file_lock(self.lock_path):
            value = _validate(load_json(self.path))
            target = next(
                (item for item in value["requests"] if item["request_id"] == request_id),
                None,
            )
            if target is None:
                raise KeyError(f"control request does not exist: {request_id}")
            target["status"] = status
            target["applied_at"] = utc_now()
            target["result"] = result
            atomic_write_json(self.path, value)
            return target


def _validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("unsupported control queue version")
    if not isinstance(value.get("next_seq"), int) or value["next_seq"] < 1:
        raise ValueError("invalid control sequence")
    requests = value.get("requests")
    if not isinstance(requests, list):
        raise ValueError("control requests must be an array")
    seen: set[str] = set()
    for item in requests:
        if not isinstance(item, dict) or item.get("kind") not in CONTROL_KINDS:
            raise ValueError("invalid control request")
        request_id = item.get("request_id")
        if not isinstance(request_id, str) or request_id in seen:
            raise ValueError("duplicate or invalid control request id")
        seen.add(request_id)
    return value
