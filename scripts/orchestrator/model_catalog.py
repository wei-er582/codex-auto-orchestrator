from __future__ import annotations

import json
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelInfo:
    slug: str
    display_name: str
    description: str
    efforts: tuple[str, ...]
    default_effort: str
    multi_agent_version: str | None
    service_tiers: tuple[str, ...]

    @property
    def family(self) -> str:
        if "sol" in self.slug.lower():
            return "sol"
        if "terra" in self.slug.lower():
            return "terra"
        return "other"

    @property
    def supports_fast(self) -> bool:
        return "priority" in self.service_tiers


class ModelCatalog:
    def __init__(self, models: dict[str, ModelInfo], source: str) -> None:
        if not models:
            raise ValueError("model catalog is empty")
        self.models = models
        self.source = source

    @classmethod
    def discover(cls, codex_command: list[str]) -> "ModelCatalog":
        completed = subprocess.run(
            [*codex_command, "debug", "models"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode == 0:
            try:
                return cls.from_payload(json.loads(completed.stdout), "codex debug models")
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                pass

        cache = Path.home() / ".codex" / "models_cache.json"
        if not cache.is_file():
            detail = completed.stderr.strip() or "model cache is missing"
            raise RuntimeError(f"unable to discover Codex models: {detail}")
        return cls.from_payload(json.loads(cache.read_text(encoding="utf-8")), str(cache))

    @classmethod
    def from_payload(cls, payload: dict, source: str = "payload") -> "ModelCatalog":
        models: dict[str, ModelInfo] = {}
        for item in payload["models"]:
            efforts = tuple(level["effort"] for level in item.get("supported_reasoning_levels", []))
            models[item["slug"]] = ModelInfo(
                slug=item["slug"],
                display_name=item.get("display_name", item["slug"]),
                description=item.get("description", ""),
                efforts=efforts,
                default_effort=item.get("default_reasoning_level", "medium"),
                multi_agent_version=item.get("multi_agent_version"),
                service_tiers=tuple(
                    str(tier.get("id"))
                    for tier in item.get("service_tiers", [])
                    if isinstance(tier, dict) and tier.get("id")
                ),
            )
        return cls(models, source)

    def require(self, model: str, effort: str) -> ModelInfo:
        if model not in self.models:
            raise ValueError(f"model is not available: {model}")
        info = self.models[model]
        if effort not in info.efforts:
            raise ValueError(f"reasoning effort {effort!r} is not supported by {model}")
        return info

    def preferred_sol(self) -> str:
        return self._preferred("sol", required_effort="max")

    def preferred_terra(self) -> str:
        return self._preferred("terra", required_effort="medium")

    def preferred_for_family(self, family: str) -> str:
        if family == "sol":
            return self.preferred_sol()
        if family == "terra":
            return self.preferred_terra()
        raise ValueError(f"unsupported model family: {family}")

    def family_for_model(self, model: str) -> str:
        return self.models[model].family

    def speed_matrix_catalog(self) -> dict[str, dict[str, object]]:
        matrix: dict[str, dict[str, object]] = {}
        for family in ("sol", "terra"):
            model = self.preferred_for_family(family)
            info = self.models[model]
            matrix[family] = {
                "model": model,
                "efforts": list(info.efforts),
                "fast_supported": info.supports_fast,
                "service_tiers": list(info.service_tiers),
            }
        return matrix

    def speed_combinations(self) -> set[str]:
        combinations: set[str] = set()
        for family, item in self.speed_matrix_catalog().items():
            model = str(item["model"])
            for effort in item["efforts"]:
                combinations.add(f"{family}:{model}:{effort}")
        return combinations

    def fingerprint(self) -> str:
        payload = json.dumps(
            self.speed_matrix_catalog(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _preferred(self, token: str, required_effort: str) -> str:
        candidates = [m for m in self.models.values() if token in m.slug and required_effort in m.efforts]
        if candidates:
            return max(candidates, key=lambda item: _model_rank(item.slug)).slug
        raise ValueError(f"no available {token.title()} model supports {required_effort}")

    def prompt_summary(self) -> list[dict[str, object]]:
        return [
            {
                "model": item.slug,
                "description": item.description,
                "efforts": list(item.efforts),
                "multi_agent_version": item.multi_agent_version or "none",
                "service_tiers": list(item.service_tiers),
            }
            for item in self.models.values()
        ]


def _model_rank(slug: str) -> tuple[tuple[int, ...], str]:
    return tuple(int(part) for part in re.findall(r"\d+", slug)), slug
