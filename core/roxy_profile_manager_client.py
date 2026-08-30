# -*- coding: utf-8 -*-
"""Strict, normalized RoxyBrowser API adapter for profile management."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import roxy_profile_manager as _manager_cfg
from config import roxybrowser as _roxy_cfg
from core.roxybrowser_client import RoxyBrowserClient

_PROFILE_OUTPUT_FIELDS = {
    "dirId", "id", "name", "windowRemark", "os", "osVersion", "coreType",
    "coreVersion", "projectId", "projectName", "labelIds", "labels",
    "openStatus", "status", "createdAt", "updateTime", "updatedAt",
    "syncBookmark", "syncHistory", "syncTab", "syncCookie",
    "syncExtensions", "syncPassword", "syncIndexedDb", "syncLocalStorage",
}
_DETAIL_OUTPUT_FIELDS = _PROFILE_OUTPUT_FIELDS | {
    "cookie", "defaultOpenUrl", "proxyInfo", "fingerInfo",
    "windowPlatformList", "startupParam", "randomFingerprint",
}
_CREATE_FIELDS = {
    "name", "os", "osVersion", "coreType", "coreVersion", "projectId",
    "windowRemark", "cookie", "defaultOpenUrl", "proxyInfo", "fingerInfo",
    "windowPlatformList", "labelIds", "syncBookmark", "syncHistory",
    "syncTab", "syncCookie", "syncExtensions", "syncPassword",
    "syncIndexedDb", "syncLocalStorage", "randomFingerprint",
}
_UPDATE_FIELDS = _CREATE_FIELDS | {"dirId"}


class RoxyProfileManagerClientError(RuntimeError):
    """A strict manager-facing Roxy API operation failed."""


@dataclass(frozen=True)
class RoxyProfileOpenResult:
    dir_id: str
    debugger_address: str
    pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dir_id": self.dir_id,
            "debugger_address": self.debugger_address,
            "pid": self.pid,
        }


def _workspace_id() -> str | int:
    raw = str(getattr(_roxy_cfg, "ROXY_WORKSPACE_ID", "") or "").strip()
    if not raw:
        raise RoxyProfileManagerClientError("ROXY_WORKSPACE_ID is required")
    return int(raw) if raw.isdigit() else raw


def _project_id() -> str | int:
    raw = str(getattr(_roxy_cfg, "ROXY_PROJECT_ID", "") or "").strip()
    return int(raw) if raw.isdigit() else raw


def _rows(payload: dict) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        rows = data.get("rows") or data.get("list") or data.get("records") or []
        return [item for item in rows if isinstance(item, dict)]
    return []


def _dir_id(item: dict[str, Any]) -> str:
    return str(item.get("dirId") or item.get("id") or item.get("profileId") or "").strip()


def _allowlist(item: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    out = {key: item[key] for key in fields if key in item}
    profile_id = _dir_id(item)
    if profile_id:
        out["dirId"] = profile_id
    return out


def _extract_debugger(payload: dict) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    value = str(
        data.get("http")
        or data.get("debuggerAddress")
        or data.get("debugger_address")
        or ""
    ).strip()
    value = value.replace("http://", "").replace("https://", "").strip("/")
    if value.startswith(":") and value[1:].isdigit():
        return f"127.0.0.1{value}"
    if value.isdigit():
        return f"127.0.0.1:{value}"
    return value


def _page_total(payload: dict) -> int | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    for key in ("total", "totalCount", "count"):
        value = data.get(key)
        if str(value or "").isdigit():
            return int(value)
    return None


class RoxyProfileManagerClient:
    def __init__(self, client: RoxyBrowserClient | None = None):
        self.client = client or RoxyBrowserClient()

    @property
    def workspace_id(self) -> str | int:
        return _workspace_id()

    @property
    def project_id(self) -> str | int:
        return _project_id()

    @property
    def owner_prefix(self) -> str:
        return str(
            getattr(_manager_cfg, "ROXY_PROFILE_MANAGER_OWNER_PREFIX", "zcode-profile-manager")
            or "zcode-profile-manager"
        ).strip()

    def list_profiles(self, *, name: str = "") -> list[dict[str, Any]]:
        page_size = 100
        page = 1
        profiles: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            params: dict[str, Any] = {
                "workspaceId": self.workspace_id,
                "page": page,
                "pageSize": page_size,
            }
            if name:
                params["name"] = str(name)
            payload = self.client.request("GET", "/browser/list_v3", params=params)
            rows = _rows(payload)
            for item in rows:
                profile = _allowlist(item, _PROFILE_OUTPUT_FIELDS)
                identity = _dir_id(profile)
                if identity and identity not in seen:
                    seen.add(identity)
                    profiles.append(profile)
            total = _page_total(payload)
            if not rows or len(rows) < page_size:
                break
            if total is not None and len(profiles) >= total:
                break
            page += 1
            if page > 1000:
                raise RoxyProfileManagerClientError(
                    "Roxy profile pagination exceeded the safety limit"
                )
        return profiles

    def get_profile(self, dir_id: str) -> dict[str, Any]:
        payload = self.client.request(
            "GET",
            "/browser/detail",
            params={"workspaceId": self.workspace_id, "dirId": str(dir_id)},
        )
        rows = _rows(payload)
        if not rows:
            raise RoxyProfileManagerClientError("Roxy profile was not found")
        return _allowlist(rows[0], _DETAIL_OUTPUT_FIELDS)

    def create_profile(
        self,
        payload: dict[str, Any],
        *,
        owner_marker: str,
    ) -> dict[str, Any]:
        body = {key: value for key, value in payload.items() if key in _CREATE_FIELDS}
        body["workspaceId"] = self.workspace_id
        if self.project_id and not body.get("projectId"):
            body["projectId"] = self.project_id
        body["windowRemark"] = self._managed_remark(
            body.get("windowRemark"), owner_marker
        )
        result = self.client.request("POST", "/browser/create", json_body=body)
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        profile_id = _dir_id(data)
        if not profile_id:
            raise RoxyProfileManagerClientError(
                "Roxy created a profile without returning dirId"
            )
        created = self.get_profile(profile_id)
        self.require_owned(created, owner_marker)
        return {"dirId": profile_id, "name": str(body.get("name") or "")}

    def update_profile(
        self,
        dir_id: str,
        payload: dict[str, Any],
        *,
        owner_marker: str,
    ) -> dict[str, Any]:
        current = self.get_profile(dir_id)
        self.require_owned(current, owner_marker)
        body = {key: value for key, value in payload.items() if key in _UPDATE_FIELDS}
        body["workspaceId"] = self.workspace_id
        body["dirId"] = str(dir_id)
        body["windowRemark"] = self._managed_remark(
            body.get("windowRemark", current.get("windowRemark")),
            owner_marker,
        )
        self.client.request("POST", "/browser/mdf", json_body=body)
        return self.get_profile(dir_id)

    def open_profile(self, dir_id: str, *, headless: bool = False) -> RoxyProfileOpenResult:
        payload = self.client.request(
            "POST",
            "/browser/open",
            json_body={
                "workspaceId": self.workspace_id,
                "dirId": str(dir_id),
                "args": [],
                "forceOpen": True,
                "headless": bool(headless),
            },
        )
        debugger_address = _extract_debugger(payload)
        if not debugger_address:
            raise RoxyProfileManagerClientError(
                "Roxy opened the profile without returning a debugger address"
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        pid = data.get("pid")
        return RoxyProfileOpenResult(
            dir_id=str(dir_id),
            debugger_address=debugger_address,
            pid=int(pid) if str(pid or "").isdigit() else None,
        )

    def close_profile(self, dir_id: str) -> None:
        self.client.request(
            "POST",
            "/browser/close",
            json_body={"workspaceId": self.workspace_id, "dirId": str(dir_id)},
        )

    def connection_info(self, dir_ids: list[str] | None = None) -> list[dict[str, Any]]:
        params = {}
        if dir_ids:
            params["dirIds"] = ",".join(str(item) for item in dir_ids)
        payload = self.client.request("GET", "/browser/connection_info", params=params)
        return [
            {
                "dirId": _dir_id(item),
                "pid": item.get("pid"),
                "http": item.get("http"),
            }
            for item in _rows(payload)
        ]

    def soft_delete_profile(self, dir_id: str, *, owner_marker: str) -> None:
        profile = self.get_profile(dir_id)
        self.require_owned(profile, owner_marker)
        self.client.request(
            "POST",
            "/browser/delete",
            json_body={
                "workspaceId": self.workspace_id,
                "dirIds": [str(dir_id)],
                "isSoftDelete": True,
            },
        )
        active_ids = {_dir_id(item) for item in self.list_profiles()}
        if str(dir_id) in active_ids:
            raise RoxyProfileManagerClientError(
                "Roxy still reports the profile as active after soft delete"
            )

    @staticmethod
    def require_owned(profile: dict[str, Any], owner_marker: str) -> None:
        remark = str(profile.get("windowRemark") or "")
        marker = f"[managed:{owner_marker}]"
        if marker not in remark:
            raise RoxyProfileManagerClientError(
                "Profile is not owned by this profile manager"
            )

    @staticmethod
    def _managed_remark(value: Any, owner_marker: str) -> str:
        marker = f"[managed:{owner_marker}]"
        remark = str(value or "").strip()
        if marker in remark:
            return remark[:500]
        return f"{marker} {remark}".strip()[:500]
