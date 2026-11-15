"""Startup refusals for unsafe operator settings (security review round 3)."""
from __future__ import annotations

import logging

import pytest

from api.app import StartupRefused, check_startup


def test_production_needs_a_web_origin():
    with pytest.raises(StartupRefused, match="THALLO_WEB_ORIGIN"):
        check_startup({})
    assert check_startup({"THALLO_WEB_ORIGIN": "https://app.example"}) == (["https://app.example"], False)


def test_development_may_run_without_an_origin():
    assert check_startup({"THALLO_ENVIRONMENT": "development"}) == ([], False)


def test_console_login_alone_is_refused():
    with pytest.raises(StartupRefused, match="THALLO_ENVIRONMENT=development"):
        check_startup({"THALLO_DEV_CONSOLE_LOGIN": "1", "THALLO_WEB_ORIGIN": "http://localhost:3000"})


def test_console_login_needs_localhost_origins_only():
    env = {"THALLO_DEV_CONSOLE_LOGIN": "1", "THALLO_ENVIRONMENT": "development"}
    for origins in ("", "https://app.example", "http://localhost:3000,https://app.example", "http://localhost.evil.test"):
        with pytest.raises(StartupRefused):
            check_startup({**env, "THALLO_WEB_ORIGIN": origins})


def test_console_login_in_development_warns_loudly(caplog):
    caplog.set_level(logging.WARNING)
    env = {"THALLO_DEV_CONSOLE_LOGIN": "1", "THALLO_ENVIRONMENT": "development",
           "THALLO_WEB_ORIGIN": "http://localhost:3000,http://127.0.0.1:3000"}
    assert check_startup(env)[1] is True
    assert "DEVELOPMENT LOGIN" in caplog.text
