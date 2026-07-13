from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model_catalog import ModelCatalog
from .util import atomic_write_json, exclusive_file_lock, load_json, utc_now


BUILTIN_PROFILES = ("balanced", "all-fast", "all-standard", "follow-entry")
SERVICE_TIERS = {"default", "priority"}
PROFILE_NAME_PATTERN = re.compile(r"^[^\x00-\x1f\\/:*?\"<>|]{1,80}$")


class SpeedConfigurationError(ValueError):
    pass


class SpeedSetupRequired(SpeedConfigurationError):
    def __init__(self, reason: str, missing: list[str] | None = None) -> None:
        self.reason = reason
        self.missing = missing or []
        detail = f": {', '.join(self.missing)}" if self.missing else ""
        super().__init__(f"speed setup required ({reason}){detail}")


@dataclass(frozen=True)
class ResolvedSpeedPolicy:
    profile_name: str
    matrix: dict[str, dict[str, str]]
    model_bindings: dict[str, str]
    catalog_fingerprint: str
    known_combinations: list[str]
    source: str
    revision: int = 1

    def tier_for(self, catalog: ModelCatalog, model: str, effort: str) -> str:
        family = catalog.family_for_model(model)
        try:
            tier = self.matrix[family][effort]
        except KeyError as exc:
            raise SpeedConfigurationError(
                f"speed matrix has no entry for {family}/{effort}"
            ) from exc
        if tier == "priority" and not catalog.models[model].supports_fast:
            raise SpeedConfigurationError(f"{model} does not advertise the Fast service tier")
        return tier

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "profile_name": self.profile_name,
            "matrix": deepcopy(self.matrix),
            "model_bindings": dict(self.model_bindings),
            "catalog_fingerprint": self.catalog_fingerprint,
            "known_combinations": list(self.known_combinations),
            "source": self.source,
            "revision": self.revision,
            "created_at": utc_now(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResolvedSpeedPolicy":
        return cls(
            profile_name=str(value["profile_name"]),
            matrix=deepcopy(value["matrix"]),
            model_bindings={str(k): str(v) for k, v in value["model_bindings"].items()},
            catalog_fingerprint=str(value["catalog_fingerprint"]),
            known_combinations=[str(item) for item in value["known_combinations"]],
            source=str(value.get("source", "job-snapshot")),
            revision=int(value.get("revision", 1)),
        )


class ProfileStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or (Path.home() / ".codex" / "orchestrator" / "config.json")).resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def read(self) -> dict[str, Any]:
        with exclusive_file_lock(self.lock_path):
            if not self.path.is_file():
                return _empty_config()
            value = load_json(self.path)
            return _validate_config(value)

    def _mutate(self, callback) -> dict[str, Any]:
        with exclusive_file_lock(self.lock_path):
            value = _validate_config(load_json(self.path)) if self.path.is_file() else _empty_config()
            callback(value)
            value["updated_at"] = utc_now()
            _validate_config(value)
            atomic_write_json(self.path, value)
            return value

    def list_profiles(
        self, catalog: ModelCatalog, entry_service_tier: str = "default"
    ) -> list[dict[str, Any]]:
        config = self.read()
        items: list[dict[str, Any]] = []
        for name in BUILTIN_PROFILES:
            items.append(
                {
                    "name": name,
                    "builtin": True,
                    "default": config.get("default_profile") == name,
                    "matrix": builtin_matrix(name, catalog, entry_service_tier),
                    "catalog_fingerprint": catalog.fingerprint(),
                }
            )
        for name, profile in config["profiles"].items():
            items.append(
                {
                    "name": name,
                    "builtin": False,
                    "default": config.get("default_profile") == name,
                    "matrix": deepcopy(profile["matrix"]),
                    "catalog_fingerprint": profile["catalog_fingerprint"],
                    "updated_at": profile["updated_at"],
                }
            )
        return items

    def save_profile(
        self,
        name: str,
        matrix: dict[str, Any],
        catalog: ModelCatalog,
        *,
        set_default: bool = False,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        _validate_profile_name(name, allow_builtin=False)
        normalized = normalize_matrix(matrix, catalog, require_complete=True)
        now = utc_now()

        def update(config: dict[str, Any]) -> None:
            existing = config["profiles"].get(name)
            if existing and not overwrite:
                raise SpeedConfigurationError(f"profile already exists: {name}")
            config["profiles"][name] = {
                "matrix": normalized,
                "known_combinations": sorted(catalog.speed_combinations()),
                "catalog_fingerprint": catalog.fingerprint(),
                "created_at": existing.get("created_at", now) if existing else now,
                "updated_at": now,
            }
            if set_default or not config.get("default_profile"):
                config["default_profile"] = name

        return self._mutate(update)

    def copy_profile(
        self,
        source: str,
        target: str,
        catalog: ModelCatalog,
        *,
        entry_service_tier: str = "default",
    ) -> dict[str, Any]:
        matrix = self.profile_matrix(source, catalog, entry_service_tier=entry_service_tier)
        return self.save_profile(target, matrix, catalog)

    def rename_profile(self, source: str, target: str) -> dict[str, Any]:
        _validate_profile_name(source, allow_builtin=False)
        _validate_profile_name(target, allow_builtin=False)

        def update(config: dict[str, Any]) -> None:
            if source not in config["profiles"]:
                raise SpeedConfigurationError(f"profile does not exist: {source}")
            if target in config["profiles"] or target in BUILTIN_PROFILES:
                raise SpeedConfigurationError(f"profile already exists: {target}")
            config["profiles"][target] = config["profiles"].pop(source)
            config["profiles"][target]["updated_at"] = utc_now()
            if config.get("default_profile") == source:
                config["default_profile"] = target

        return self._mutate(update)

    def delete_profile(self, name: str) -> dict[str, Any]:
        _validate_profile_name(name, allow_builtin=False)

        def update(config: dict[str, Any]) -> None:
            if name not in config["profiles"]:
                raise SpeedConfigurationError(f"profile does not exist: {name}")
            if config.get("default_profile") == name:
                raise SpeedConfigurationError("cannot delete the default profile")
            del config["profiles"][name]

        return self._mutate(update)

    def set_default(self, name: str) -> dict[str, Any]:
        _validate_profile_name(name, allow_builtin=False)

        def update(config: dict[str, Any]) -> None:
            if name not in config["profiles"]:
                raise SpeedConfigurationError(f"profile does not exist: {name}")
            config["default_profile"] = name

        return self._mutate(update)

    def profile_matrix(
        self,
        name: str,
        catalog: ModelCatalog,
        *,
        entry_service_tier: str = "default",
    ) -> dict[str, dict[str, str]]:
        if name in BUILTIN_PROFILES:
            return builtin_matrix(name, catalog, entry_service_tier)
        config = self.read()
        try:
            return normalize_matrix(config["profiles"][name]["matrix"], catalog, require_complete=True)
        except KeyError as exc:
            raise SpeedConfigurationError(f"profile does not exist: {name}") from exc

    def resolve(
        self,
        catalog: ModelCatalog,
        name: str | None = None,
        *,
        entry_service_tier: str = "default",
        require_user_default: bool = True,
    ) -> ResolvedSpeedPolicy:
        config = self.read()
        selected = name or config.get("default_profile")
        if require_user_default and not config.get("default_profile"):
            raise SpeedSetupRequired("first_setup")
        if not selected:
            raise SpeedSetupRequired("first_setup")
        if selected in BUILTIN_PROFILES:
            matrix = builtin_matrix(selected, catalog, entry_service_tier)
            known = sorted(catalog.speed_combinations())
            source = "builtin"
        else:
            try:
                profile = config["profiles"][selected]
            except KeyError as exc:
                raise SpeedConfigurationError(f"profile does not exist: {selected}") from exc
            missing = sorted(catalog.speed_combinations() - set(profile["known_combinations"]))
            if missing:
                raise SpeedSetupRequired("catalog_changed", missing)
            matrix = normalize_matrix(profile["matrix"], catalog, require_complete=True)
            known = sorted(profile["known_combinations"])
            source = "profile"
        bindings = {
            family: str(item["model"])
            for family, item in catalog.speed_matrix_catalog().items()
        }
        return ResolvedSpeedPolicy(
            profile_name=selected,
            matrix=matrix,
            model_bindings=bindings,
            catalog_fingerprint=catalog.fingerprint(),
            known_combinations=known,
            source=source,
        )


def builtin_matrix(
    name: str, catalog: ModelCatalog, entry_service_tier: str = "default"
) -> dict[str, dict[str, str]]:
    if name not in BUILTIN_PROFILES:
        raise SpeedConfigurationError(f"unknown built-in profile: {name}")
    entry = canonical_service_tier(entry_service_tier)
    matrix: dict[str, dict[str, str]] = {}
    for family, item in catalog.speed_matrix_catalog().items():
        matrix[family] = {}
        for effort in item["efforts"]:
            tier = "default"
            if name == "all-fast":
                tier = "priority"
            elif name == "follow-entry":
                tier = entry
            elif name == "balanced" and (
                (family == "sol" and effort in {"max", "ultra"})
                or (family == "terra" and effort == "ultra")
            ):
                tier = "priority"
            if tier == "priority" and not bool(item["fast_supported"]):
                tier = "default"
            matrix[family][str(effort)] = tier
    return matrix


def normalize_matrix(
    value: dict[str, Any], catalog: ModelCatalog, *, require_complete: bool
) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise SpeedConfigurationError("speed matrix must be an object")
    expected = catalog.speed_matrix_catalog()
    extra_families = set(value) - set(expected)
    if extra_families:
        raise SpeedConfigurationError(f"unknown model families: {sorted(extra_families)}")
    normalized: dict[str, dict[str, str]] = {}
    for family, item in expected.items():
        raw_family = value.get(family)
        if not isinstance(raw_family, dict):
            if require_complete:
                raise SpeedConfigurationError(f"missing matrix for {family}")
            raw_family = {}
        efforts = {str(effort) for effort in item["efforts"]}
        extras = set(raw_family) - efforts
        if extras:
            raise SpeedConfigurationError(
                f"unsupported reasoning levels for {family}: {sorted(extras)}"
            )
        normalized[family] = {}
        for effort in item["efforts"]:
            if effort not in raw_family:
                if require_complete:
                    raise SpeedConfigurationError(f"missing speed cell: {family}/{effort}")
                tier = "default"
            else:
                tier = canonical_service_tier(raw_family[effort])
            if tier == "priority" and not bool(item["fast_supported"]):
                raise SpeedConfigurationError(f"Fast is unavailable for {item['model']}")
            normalized[family][str(effort)] = tier
    return normalized


def parse_matrix_text(text: str, catalog: ModelCatalog) -> dict[str, dict[str, str]]:
    fast: dict[str, list[str]] = {}
    for family in ("sol", "terra"):
        match = re.search(
            rf"(?im)^\s*{family}\s*(?:fast|快速)?\s*=\s*([^\r\n]*)$", text
        )
        if not match:
            raise SpeedConfigurationError(f"missing '{family} Fast = ...' line")
        values = [item.strip().lower() for item in re.split(r"[,，\s]+", match.group(1)) if item.strip()]
        available = set(catalog.speed_matrix_catalog()[family]["efforts"])
        unknown = set(values) - available
        if unknown:
            raise SpeedConfigurationError(
                f"unsupported reasoning levels for {family}: {sorted(unknown)}"
            )
        fast[family] = values
    matrix = builtin_matrix("all-standard", catalog)
    for family, efforts in fast.items():
        for effort in efforts:
            matrix[family][effort] = "priority"
    return normalize_matrix(matrix, catalog, require_complete=True)


def format_matrix(matrix: dict[str, dict[str, str]]) -> str:
    lines: list[str] = []
    for family in ("sol", "terra"):
        values = matrix.get(family, {})
        fast = [effort for effort, tier in values.items() if tier == "priority"]
        standard = [effort for effort, tier in values.items() if tier == "default"]
        lines.append(f"{family.title()} Fast = {', '.join(fast) or '(none)'}")
        lines.append(f"{family.title()} Standard = {', '.join(standard) or '(none)'}")
    return "\n".join(lines)


def canonical_service_tier(value: Any) -> str:
    if isinstance(value, bool):
        return "priority" if value else "default"
    text = str(value).strip().lower()
    if text in {"fast", "priority"}:
        return "priority"
    if text in {"standard", "normal", "default", "普通"}:
        return "default"
    raise SpeedConfigurationError(f"unsupported service tier: {value!r}")


def _empty_config() -> dict[str, Any]:
    return {
        "version": 1,
        "prompt_policy": "explicit",
        "default_profile": "",
        "profiles": {},
        "updated_at": utc_now(),
    }


def _validate_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise SpeedConfigurationError("unsupported orchestrator config version")
    if value.get("prompt_policy") != "explicit":
        raise SpeedConfigurationError("prompt_policy must be explicit")
    if not isinstance(value.get("default_profile", ""), str):
        raise SpeedConfigurationError("default_profile must be a string")
    profiles = value.get("profiles")
    if not isinstance(profiles, dict):
        raise SpeedConfigurationError("profiles must be an object")
    for name, profile in profiles.items():
        _validate_profile_name(name, allow_builtin=False)
        if not isinstance(profile, dict) or not isinstance(profile.get("matrix"), dict):
            raise SpeedConfigurationError(f"invalid profile: {name}")
        if not isinstance(profile.get("known_combinations"), list) or not all(
            isinstance(item, str) for item in profile["known_combinations"]
        ):
            raise SpeedConfigurationError(f"invalid known_combinations for profile: {name}")
        for field in ("catalog_fingerprint", "created_at", "updated_at"):
            if not isinstance(profile.get(field), str):
                raise SpeedConfigurationError(f"invalid {field} for profile: {name}")
    default = value.get("default_profile")
    if default and default not in profiles:
        raise SpeedConfigurationError("default profile must be a saved user profile")
    return value


def _validate_profile_name(name: str, *, allow_builtin: bool) -> None:
    if not isinstance(name, str) or not PROFILE_NAME_PATTERN.fullmatch(name.strip()):
        raise SpeedConfigurationError("profile name is empty, too long, or contains path characters")
    if not allow_builtin and name in BUILTIN_PROFILES:
        raise SpeedConfigurationError(f"built-in profile is immutable: {name}")
