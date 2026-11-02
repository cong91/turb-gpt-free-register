"""Provider-neutral Codex OAuth callback handling and credential persistence."""
from __future__ import annotations


def finish_codex_callback(
    *,
    email: str,
    auth_source: str,
    auth_url: str,
    callback_url: str,
    state: str,
    code: str,
    code_verifier: str,
    sub2_auth: dict | None,
    proxy: str | None,
    preserve_session: bool,
    allow_callback_receipt: bool = False,
) -> dict:
    """Finalize CPA, sub2, or local OAuth without exposing callback secrets."""
    from core import codex_oauth as proto

    if auth_source == "cpa":
        payload = proto._submit_cpa_callback(callback_url)
        path = proto._save_cpa_local_record(
            email=email,
            callback_url=callback_url,
            auth_url=auth_url,
            state=state,
            submit_payload=payload,
        )
        message = payload.get("message") or payload.get("status_message") or "CPA callback submitted"
        if allow_callback_receipt and not proto._extract_cpa_auth_json(payload):
            return proto._codex_result(
                status="receipt", ok=True, email=email,
                message="Callback OAuth đã gửi; chưa có credential cục bộ.",
            )
        return proto._codex_result(
            status="success", ok=True, email=email, file_path=str(path) if path else None,
            callback_url=callback_url, message=str(message),
        )

    if auth_source == "sub2":
        payload = proto._submit_sub2_callback(
            callback_url,
            session_id=(sub2_auth or {}).get("session_id", ""),
            redirect_uri=(proto.parse_qs(proto.urlparse(auth_url or "").query).get("redirect_uri") or [""])[0],
        )
        path = proto._save_sub2_local_record(
            email=email,
            callback_url=callback_url,
            auth_url=auth_url,
            state=state,
            submit_payload=payload,
        )
        message = payload.get("message") or payload.get("status_message") or "sub2 callback uploaded"
        if allow_callback_receipt and not proto._extract_cpa_auth_json(payload):
            return proto._codex_result(
                status="receipt", ok=True, email=email,
                message="Callback OAuth đã gửi; chưa có credential cục bộ.",
            )
        return proto._codex_result(
            status="success", ok=True, email=email, file_path=str(path) if path else None,
            callback_url=callback_url, message=str(message),
        )

    if auth_source != "local" or not code_verifier:
        raise RuntimeError("Codex local OAuth is missing its verifier")
    session_proxy = "" if preserve_session and proxy is None else proxy
    session = proto.BrowserSession(
        proxy=session_proxy,
        fingerprint_seed=f"account:{email.lower()}",
    )
    try:
        token_payload = proto.exchange_codex_token(session, code, code_verifier)
    finally:
        from core.session import close_browser_session
        close_browser_session(session)
    claims = proto._parse_id_token(token_payload.get("id_token", ""))
    claimed_email = str(claims.get("email") or "").strip()
    if preserve_session and claimed_email.casefold() != email.strip().casefold():
        raise RuntimeError("Codex identity could not be verified for the requested account")
    effective_email = claimed_email or email
    storage = proto.build_codex_storage(token_payload, claims)
    path = proto.save_codex_credential(storage, effective_email, claims.get("plan_type", ""))
    return proto._codex_result(
        status="success", ok=True, email=effective_email, file_path=str(path), callback_url=callback_url,
    )
