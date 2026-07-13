from __future__ import annotations

import html
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .model_catalog import ModelCatalog
from .speed_profiles import (
    BUILTIN_PROFILES,
    ProfileStore,
    ResolvedSpeedPolicy,
    SpeedConfigurationError,
    builtin_matrix,
    normalize_matrix,
)
from .state import JobStore


MAX_REQUEST_BYTES = 64 * 1024
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")


@dataclass(frozen=True)
class SetupResult:
    status: str
    profile_name: str = ""
    message: str = ""


class SpeedSetupServer:
    def __init__(
        self,
        *,
        catalog: ModelCatalog,
        profiles: ProfileStore,
        entry_context: dict[str, Any] | None = None,
        job_store: JobStore | None = None,
        selected_profile: str = "balanced",
        reason: str = "configure",
        timeout_seconds: int = 600,
        port: int = 0,
        token: str | None = None,
        csrf: str | None = None,
        on_complete: Callable[[SetupResult], None] | None = None,
    ) -> None:
        self.catalog = catalog
        self.profiles = profiles
        self.entry_context = entry_context or {}
        self.job_store = job_store
        self.selected_profile = selected_profile
        self.reason = reason
        self.timeout_seconds = timeout_seconds
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self.csrf = csrf or secrets.token_urlsafe(32)
        self.on_complete = on_complete
        self.result = SetupResult("pending")
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        if not self.port:
            raise RuntimeError("speed setup server has not bound a port")
        return f"http://127.0.0.1:{self.port}/?token={urllib.parse.quote(self.token)}"

    def serve(self, *, open_browser: bool = True) -> SetupResult:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "CodexSpeedSetup/1.0"

            def do_GET(self) -> None:  # noqa: N802
                owner._handle_get(self)

            def do_POST(self) -> None:  # noqa: N802
                owner._handle_post(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.timeout = 0.5
        self.port = int(self._server.server_address[1])
        if open_browser:
            webbrowser.open(self.url, new=1, autoraise=True)
        deadline = time.monotonic() + self.timeout_seconds
        while self.result.status == "pending" and time.monotonic() < deadline:
            self._server.handle_request()
        if self.result.status == "pending":
            self.result = SetupResult("timeout", message="speed setup timed out")
        self._server.server_close()
        if self.on_complete:
            self.on_complete(self.result)
        return self.result

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._valid_host(handler.headers.get("Host", "")):
            return _send(handler, 403, "Invalid host")
        parsed = urllib.parse.urlparse(handler.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path != "/" or query.get("token", [""])[0] != self.token:
            return _send(handler, 403, "Invalid token")
        _send(handler, 200, self._render(), content_type="text/html; charset=utf-8")

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._valid_host(handler.headers.get("Host", "")):
            return _send(handler, 403, "Invalid host")
        allowed_origin = f"http://127.0.0.1:{self.port}"
        if handler.headers.get("Origin", "") != allowed_origin:
            return _send(handler, 403, "Invalid origin")
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            return _send(handler, 400, "Invalid request size")
        if length <= 0 or length > MAX_REQUEST_BYTES:
            return _send(handler, 413, "Request is too large")
        body = handler.rfile.read(length).decode("utf-8", errors="strict")
        fields = urllib.parse.parse_qs(body, keep_blank_values=True, max_num_fields=128)
        if fields.get("token", [""])[0] != self.token or fields.get("csrf", [""])[0] != self.csrf:
            return _send(handler, 403, "Invalid form token")
        if fields.get("action", [""])[0] == "cancel":
            self.result = SetupResult("cancelled", message="speed setup cancelled")
            _send(handler, 200, _finished_page("已取消本次编排。"), content_type="text/html; charset=utf-8")
            return
        try:
            result = self._apply_form(fields)
        except (SpeedConfigurationError, KeyError, ValueError) as exc:
            return _send(handler, 400, self._render(error=str(exc)), content_type="text/html; charset=utf-8")
        self.result = result
        _send(
            handler,
            200,
            _finished_page("速度配置已保存，编排器将继续执行。"),
            content_type="text/html; charset=utf-8",
        )

    def _apply_form(self, fields: dict[str, list[str]]) -> SetupResult:
        catalog_view = self.catalog.speed_matrix_catalog()
        action = fields.get("action", ["save"])[0]
        if action in {"copy-profile", "rename-profile", "delete-profile", "set-default-profile"}:
            if self.job_store or self.reason != "profile_configuration":
                raise SpeedConfigurationError("profile management is available only in profile configuration")
            selected = fields.get("selected_profile", [""])[0].strip()
            target = fields.get("target_profile", [""])[0].strip()
            if action == "copy-profile":
                self.profiles.copy_profile(
                    selected,
                    target,
                    self.catalog,
                    entry_service_tier=self.entry_context.get("service_tier", "default"),
                )
                return SetupResult("saved", profile_name=target)
            if action == "rename-profile":
                self.profiles.rename_profile(selected, target)
                return SetupResult("saved", profile_name=target)
            if action == "delete-profile":
                self.profiles.delete_profile(selected)
                return SetupResult("saved", profile_name=selected)
            self.profiles.set_default(selected)
            return SetupResult("saved", profile_name=selected)
        if action == "use-profile":
            if not self.job_store:
                raise SpeedConfigurationError("using a profile directly requires an active job")
            if self.reason in {"first_setup", "catalog_changed"}:
                raise SpeedConfigurationError("this setup must save the required profile changes first")
            selected = fields.get("selected_profile", [""])[0].strip()
            resolved = self.profiles.resolve(
                self.catalog,
                selected,
                entry_service_tier=self.entry_context.get("service_tier", "default"),
            )
            self.job_store.set_speed_policy(resolved.to_dict())
            self.job_store.set_desired_status("running")
            self.job_store.set_checkpoint(phase="speed-configured", safe=True)
            return SetupResult("saved", profile_name=resolved.profile_name)
        raw: dict[str, dict[str, str]] = {}
        for family, item in catalog_view.items():
            raw[family] = {}
            for effort in item["efforts"]:
                key = f"fast__{family}__{effort}"
                raw[family][str(effort)] = "priority" if key in fields else "default"
        matrix = normalize_matrix(raw, self.catalog, require_complete=True)
        scope = fields.get("scope", ["job" if self.job_store else "new-profile"])[0]
        name = fields.get("profile_name", [""])[0].strip()
        if self.reason == "first_setup" and scope != "save-default":
            raise SpeedConfigurationError("首次设置必须保存一份命名配置并设为默认")
        if self.reason == "catalog_changed" and scope == "job":
            raise SpeedConfigurationError("新模型或新推理档必须先更新命名配置")
        set_default = scope == "save-default"

        if scope == "job":
            if not self.job_store:
                raise SpeedConfigurationError("job-only scope requires an active job")
            resolved = ResolvedSpeedPolicy(
                profile_name="job-override",
                matrix=matrix,
                model_bindings={
                    family: str(item["model"]) for family, item in catalog_view.items()
                },
                catalog_fingerprint=self.catalog.fingerprint(),
                known_combinations=sorted(self.catalog.speed_combinations()),
                source="job-override",
            )
        else:
            if not name:
                raise SpeedConfigurationError("请输入配置名称")
            overwrite = scope == "update-profile"
            self.profiles.save_profile(
                name,
                matrix,
                self.catalog,
                set_default=set_default,
                overwrite=overwrite,
            )
            resolved = self.profiles.resolve(
                self.catalog, name, entry_service_tier=self.entry_context.get("service_tier", "default")
            )
        if self.job_store:
            self.job_store.set_speed_policy(resolved.to_dict())
            self.job_store.set_desired_status("running")
            self.job_store.set_checkpoint(phase="speed-configured", safe=True)
        return SetupResult("saved", profile_name=resolved.profile_name)

    def _render(self, error: str = "") -> str:
        catalog_view = self.catalog.speed_matrix_catalog()
        try:
            initial = self.profiles.profile_matrix(
                self.selected_profile,
                self.catalog,
                entry_service_tier=self.entry_context.get("service_tier", "default"),
            )
        except SpeedConfigurationError:
            saved = self.profiles.read().get("profiles", {}).get(self.selected_profile, {})
            if isinstance(saved.get("matrix"), dict):
                initial = normalize_matrix(saved["matrix"], self.catalog, require_complete=False)
            else:
                initial = builtin_matrix("balanced", self.catalog)
        available_efforts = {
            str(effort)
            for item in catalog_view.values()
            for effort in item["efforts"]
        }
        efforts = [effort for effort in EFFORT_ORDER if effort in available_efforts]
        efforts.extend(sorted(available_efforts - set(EFFORT_ORDER)))
        header = "".join(f"<th>{html.escape(effort.title())}</th>" for effort in efforts)
        rows: list[str] = []
        saved_profile = self.profiles.read().get("profiles", {}).get(self.selected_profile, {})
        known = set(saved_profile.get("known_combinations", [])) if isinstance(saved_profile, dict) else set()
        new_combinations = self.catalog.speed_combinations() - known if self.reason == "catalog_changed" else set()
        for family in ("sol", "terra"):
            item = catalog_view[family]
            cells: list[str] = []
            for effort in efforts:
                supported = effort in item["efforts"] and bool(item["fast_supported"])
                checked = initial.get(family, {}).get(effort) == "priority"
                attrs = " checked" if checked and supported else ""
                if not supported:
                    attrs += " disabled"
                marker = (
                    '<span class="new-cell">新增</span>'
                    if f"{family}:{item['model']}:{effort}" in new_combinations
                    else ""
                )
                cells.append(
                    f'<td><label><input type="checkbox" name="fast__{family}__{effort}"{attrs}> Fast</label>{marker}</td>'
                )
            rows.append(
                f"<tr><th>{family.title()}<small>{html.escape(str(item['model']))}</small></th>{''.join(cells)}</tr>"
            )
        entry = self.entry_context
        if self.reason == "first_setup":
            scopes = '<option value="save-default">保存并设为默认</option>'
        elif self.reason == "catalog_changed":
            scopes = '<option value="update-profile">更新当前命名配置并应用</option>'
        else:
            scopes = (
                '<option value="job">仅本作业应用</option>' if self.job_store else ""
            ) + (
                '<option value="new-profile">保存为新的命名配置</option>'
                '<option value="update-profile">更新同名配置并应用</option>'
                '<option value="save-default">保存并设为默认</option>'
            )
        profile_value = "" if self.selected_profile in BUILTIN_PROFILES else self.selected_profile
        profile_items = self.profiles.list_profiles(
            self.catalog,
            self.entry_context.get("service_tier", "default"),
        )
        profile_options = "".join(
            f'<option value="{html.escape(str(item["name"]))}"'
            f'{" selected" if item["name"] == self.selected_profile else ""}>'
            f'{html.escape(str(item["name"]))}{"（内置）" if item["builtin"] else ""}'
            "</option>"
            for item in profile_items
        )
        quick_use = ""
        if self.job_store and self.reason == "job_customization":
            quick_use = (
                '<div class="quick"><label>使用配置 <select name="selected_profile">'
                + profile_options
                + '</select></label><button class="primary" name="action" value="use-profile">'
                '使用所选配置并开始</button></div>'
            )
        profile_management = ""
        if not self.job_store and self.reason == "profile_configuration":
            profile_management = (
                '<div class="manager"><h2>管理命名配置</h2><label>现有配置 <select name="selected_profile">'
                + profile_options
                + '</select></label><label>新名称 <input type="text" name="target_profile" placeholder="复制或重命名时填写"></label>'
                '<div class="actions"><button name="action" value="copy-profile">复制</button>'
                '<button name="action" value="rename-profile">重命名</button>'
                '<button name="action" value="set-default-profile">设为默认</button>'
                '<button class="secondary" name="action" value="delete-profile">删除</button></div></div>'
            )
        new_notice = ""
        if new_combinations:
            new_notice = (
                '<p class="new-notice">发现新组合：'
                + html.escape(", ".join(sorted(new_combinations)))
                + "。新格子默认普通档，请确认后更新当前命名配置。</p>"
            )
        matrix_table = (
            f"<table><thead><tr><th>模型</th>{header}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
        if self.reason == "catalog_changed":
            matrix_table = (
                "<details><summary>展开完整 Sol/Terra 矩阵并设置新格子</summary>"
                + matrix_table
                + "</details>"
            )
        error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Codex Auto Orchestrator 速度配置</title>
<style>
body{{font-family:Segoe UI,system-ui,sans-serif;background:#f4f6fb;color:#1f2937;margin:0;padding:32px}}
.card{{max-width:980px;margin:auto;background:#fff;border-radius:16px;padding:28px;box-shadow:0 10px 32px #1f29371a}}
h1{{margin-top:0}} .meta{{background:#eef2ff;padding:12px 16px;border-radius:10px;line-height:1.7}}
table{{border-collapse:collapse;width:100%;margin:20px 0}} th,td{{border:1px solid #d9deea;padding:12px;text-align:center}}
th small{{display:block;font-weight:400;color:#6b7280;margin-top:4px}} input[type=text],select{{padding:10px;width:min(420px,90%);margin:6px}}
.actions{{display:flex;gap:12px;margin-top:18px}} button{{border:0;border-radius:9px;padding:11px 18px;cursor:pointer}}
.primary{{background:#4f46e5;color:#fff}} .secondary{{background:#e5e7eb}} .error{{background:#fee2e2;color:#991b1b;padding:12px;border-radius:8px}}
.quick,.manager{{background:#f8fafc;padding:12px;border-radius:10px;margin:16px 0}} .quick{{display:flex;gap:12px;align-items:center}} .new-cell{{display:block;color:#b45309;font-size:12px}} .new-notice{{background:#fff7ed;color:#9a3412;padding:12px;border-radius:8px}}
.note{{color:#6b7280}} input:disabled+span{{color:#9ca3af}}
</style></head><body><main class="card"><h1>编排速度配置</h1>{error_html}
<div class="meta">入口：{html.escape(str(entry.get('model','unknown')))} / {html.escape(str(entry.get('reasoning','unknown')))} / {html.escape(str(entry.get('service_tier','default')))}<br>
规划器固定使用最新 Sol + Max；其速度取 Sol/Max 格子。当前原因：{html.escape(self.reason)}</div>
<p class="note">勾选 Fast 会使用 <code>service_tier=&quot;priority&quot;</code>，速度更快但使用量更高；未勾选会显式使用普通档。</p>
<form method="post" action="/"><input type="hidden" name="token" value="{html.escape(self.token)}"><input type="hidden" name="csrf" value="{html.escape(self.csrf)}">
{quick_use}{profile_management}{new_notice}{matrix_table}
<label>配置名称 <input type="text" name="profile_name" value="{html.escape(profile_value)}" placeholder="例如：日常开发"></label><br>
<label>保存范围 <select name="scope">{scopes}</select></label>
<div class="actions"><button class="primary" name="action" value="save">保存并开始</button><button class="secondary" name="action" value="cancel">取消本次编排</button></div>
</form></main></body></html>"""

    def _valid_host(self, host: str) -> bool:
        return host in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

def _send(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: str,
    *,
    content_type: str = "text/plain; charset=utf-8",
) -> None:
    payload = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
    handler.end_headers()
    handler.wfile.write(payload)


def _finished_page(message: str) -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>完成</title>"
        f"<body style=\"font-family:Segoe UI,sans-serif;padding:40px\"><h2>{html.escape(message)}</h2>"
        "<p>此一次性页面现在可以关闭。</p></body></html>"
    )
