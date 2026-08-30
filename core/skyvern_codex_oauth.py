# -*- coding: utf-8 -*-
"""Skyvern 云端浏览器 Codex OAuth 入口。"""
from __future__ import annotations

from core.browser_use_codex_oauth import run_browser_use_codex_oauth
from core.codex_login_credentials import CodexLoginCredentials


def run_skyvern_codex_oauth(
    email: str,
    otp_provider=None,
    proxy: str | None = None,
    force: bool = False,
    credentials: CodexLoginCredentials | None = None,
    existing_browser=None,
    existing_context=None,
    existing_page=None,
    existing_session_info=None,
    fresh_browser_profile: bool = False,
) -> dict:
    return run_browser_use_codex_oauth(
        email=email,
        otp_provider=otp_provider,
        proxy=proxy,
        force=force,
        cloud_provider="skyvern",
        credentials=credentials,
        existing_browser=existing_browser,
        existing_context=existing_context,
        existing_page=existing_page,
        existing_session_info=existing_session_info,
        fresh_browser_profile=fresh_browser_profile,
    )
