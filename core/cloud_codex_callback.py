"""Cloud-provider callback entry point using the shared Codex finalizer."""
from __future__ import annotations


def finish_cloud_codex_callback(
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
) -> dict:
    from core import codex_oauth as proto
    from core.codex_callback_finalizer import finish_codex_callback

    return finish_codex_callback(
        email=email,
        auth_source=auth_source,
        auth_url=auth_url,
        callback_url=callback_url,
        state=state,
        code=code,
        code_verifier=code_verifier,
        sub2_auth=sub2_auth,
        proxy=proxy,
        preserve_session=preserve_session,
        allow_callback_receipt=True,
    )
