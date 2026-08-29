# -*- coding: utf-8 -*-
"""ViOTP HTTP client for renting phone numbers and reading SMS sessions."""
from urllib.parse import urljoin


class ViOtpClientError(RuntimeError):
    """ViOTP request or response error."""

    def __init__(self, message: str, *, status_code: str = ""):
        super().__init__(message)
        self.status_code = status_code


def _url(api_base: str, path: str) -> str:
    api_base = str(api_base or "").strip()
    if not api_base:
        raise ViOtpClientError("VIOTP_API_BASE 不能为空")
    return urljoin(api_base.rstrip("/") + "/", path.lstrip("/"))


def _request(http, api_base: str, token: str, path: str, params: dict) -> dict:
    token = str(token or "").strip()
    if not token:
        raise ViOtpClientError("VIOTP_API_TOKEN 不能为空")
    query = {"token": token}
    query.update(params)
    resp = http.get(_url(api_base, path), params=query)
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception as exc:
        raise ViOtpClientError(f"ViOTP 响应不是 JSON：{text[:200]}") from exc
    if resp.status_code != 200 or not isinstance(data, dict):
        raise ViOtpClientError(f"ViOTP HTTP {resp.status_code}: {text[:200]}")
    if not data.get("success"):
        code = str(data.get("status_code") or "")
        message = str(data.get("message") or "请求失败")
        raise ViOtpClientError(
            f"ViOTP 请求失败 [{code}]：{message}",
            status_code=code,
        )
    return data.get("data") or {}


def _service_score(name: str) -> int:
    normalized = str(name or "").lower()
    has_openai = "openai" in normalized
    has_chatgpt = "chatgpt" in normalized
    if has_openai and has_chatgpt:
        return 100
    if has_chatgpt:
        return 95
    if has_openai:
        return 90
    if "codex" in normalized:
        return 85
    if "gpt" in normalized:
        return 70
    return 0


def select_openai_service(
    http,
    *,
    api_base: str,
    token: str,
    country: str = "vn",
) -> dict:
    params = {}
    if str(country or "").strip():
        params["country"] = str(country).strip()
    services = _request(http, api_base, token, "/service/getv2", params)
    if not isinstance(services, list):
        raise ViOtpClientError("ViOTP services data 不是 JSON 数组")

    candidates = []
    for service in services:
        if not isinstance(service, dict):
            continue
        try:
            service_id = int(service.get("id"))
            price = float(service.get("price") or 0)
        except (TypeError, ValueError):
            continue
        score = _service_score(service.get("name"))
        if service_id > 0 and score > 0:
            candidates.append((score, -price, service_id, service))
    if not candidates:
        raise ViOtpClientError("ViOTP 未找到 OpenAI/ChatGPT/Codex 服务")
    return max(candidates, key=lambda item: item[:3])[3]


def acquire_number(
    http,
    *,
    api_base: str,
    token: str,
    service_id: str,
    country: str = "",
    network: str = "",
) -> tuple[str, str]:
    service_id = str(service_id or "").strip()
    if not service_id:
        raise ViOtpClientError("VIOTP_SERVICE_ID 不能为空")
    params = {"serviceId": service_id}
    if str(country or "").strip():
        params["country"] = str(country).strip()
    if str(network or "").strip():
        params["network"] = str(network).strip()
    data = _request(http, api_base, token, "/request/getv2", params)
    if not isinstance(data, dict):
        raise ViOtpClientError("ViOTP number data 不是 JSON 对象")
    request_id = str(data.get("request_id") or "").strip()
    phone = "".join(
        ch for ch in str(data.get("re_phone_number") or data.get("phone_number") or "")
        if ch.isdigit()
    )
    if not data.get("re_phone_number"):
        country_code = "".join(ch for ch in str(data.get("countryCode") or "") if ch.isdigit())
        if country_code and phone and not phone.startswith(country_code):
            phone = f"{country_code}{phone}"
    if not request_id or not phone:
        raise ViOtpClientError(
            f"ViOTP 响应缺少 request_id/phone_number：{str(data)[:200]}"
        )
    return request_id, phone


def get_session(http, *, api_base: str, token: str, request_id: str) -> tuple[int, str]:
    data = _request(
        http,
        api_base,
        token,
        "/session/getv2",
        {"requestId": request_id},
    )
    if not isinstance(data, dict):
        raise ViOtpClientError("ViOTP session data 不是 JSON 对象")
    try:
        status = int(data.get("Status", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ViOtpClientError(f"ViOTP session 状态无效：{data.get('Status')!r}") from exc
    return status, str(data.get("Code") or "").strip()
