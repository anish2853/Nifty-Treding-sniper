"""Unit tests for SmartAPI callback service, authentication state, and execution guard.

Verifies:
- /health endpoint
- /smartapi/callback with missing parameters
- /smartapi/callback with valid parameters
- /smartapi/callback with error parameters
- Secret redaction (tokens never exposed in logs, summaries, or JSON)
- ExecutionRouter paper-only guarantee
- Prevention of live order placement
- State lifecycle transitions
"""
import pytest
from fastapi.testclient import TestClient

import settings
from backend.auth_state import (
    SmartApiAuthState,
    SmartApiAuthStatus,
    get_auth_state,
    get_smartapi_login_url,
    mask_identifier,
)
from backend.smartapi_callback import app
from engines.execution_router import (
    ExecutionMode,
    ExecutionRouter,
    ExecutionSecurityError,
    SmartApiExecutor,
    get_execution_router,
)


@pytest.fixture(autouse=True)
def reset_auth_state():
    """Ensure clean auth state before and after each test."""
    auth = get_auth_state()
    auth.clear()
    yield
    auth.clear()


@pytest.fixture
def client():
    return TestClient(app)


def test_health_check(client):
    """Requirement 11: GET /health returns {'status': 'ok'}."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data == {"status": "ok"}


def test_root_index(client):
    """Root GET / returns 200 and friendly HTML landing page."""
    response = client.get("/")
    assert response.status_code == 200
    assert "NIFTY SNIPER Gateway" in response.text
    assert "/health" in response.text
    assert "/smartapi/callback" in response.text


def test_favicon(client):
    """GET /favicon.ico returns 204 No Content."""
    response = client.get("/favicon.ico")
    assert response.status_code == 204


def test_callback_missing_parameters(client):
    """Callback with no parameters must return 400 and an informative error page."""
    response = client.get("/smartapi/callback")
    assert response.status_code == 400
    assert "Missing Authentication Parameters" in response.text
    auth = get_auth_state()
    assert auth.status == SmartApiAuthStatus.ERROR
    assert not auth.is_authenticated()


def test_callback_valid_expected_parameters(client):
    """Callback with valid tokens must transition state to AUTHENTICATED and return 200."""
    sample_auth = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy_token_xyz"
    sample_feed = "feed_token_secure_999"
    sample_refresh = "refresh_token_secure_888"

    response = client.get(
        "/smartapi/callback",
        params={
            "auth_token": sample_auth,
            "feed_token": sample_feed,
            "refresh_token": sample_refresh,
            "client_code": "AB12345",
        },
    )

    assert response.status_code == 200
    assert "SmartAPI Connected Successfully" in response.text
    # Sensitive tokens must NOT appear in HTML response body
    assert sample_auth not in response.text
    assert sample_feed not in response.text
    assert sample_refresh not in response.text

    # Verify backend auth state
    auth = get_auth_state()
    assert auth.status == SmartApiAuthStatus.AUTHENTICATED
    assert auth.is_authenticated() is True
    assert auth.client_code == "AB****45"
    assert auth.get_auth_token() == sample_auth
    assert auth.get_feed_token() == sample_feed


def test_callback_with_request_token(client):
    """Callback supporting request_token parameter."""
    sample_req_token = "req_token_77777"
    response = client.get(
        "/smartapi/callback",
        params={"request_token": sample_req_token, "client_id": "CLIENT99"},
    )
    assert response.status_code == 200
    auth = get_auth_state()
    assert auth.is_authenticated() is True
    assert auth.client_code == "CL****99"


def test_callback_invalid_or_rejected(client):
    """SmartAPI rejection or user cancel must set ERROR state and return 400."""
    response = client.get(
        "/smartapi/callback",
        params={
            "status": "false",
            "message": "Invalid MPIN / TOTP entered by user",
        },
    )
    assert response.status_code == 400
    assert "Authentication Failed" in response.text
    assert "Invalid MPIN / TOTP" in response.text
    auth = get_auth_state()
    assert auth.status == SmartApiAuthStatus.ERROR
    assert auth.is_authenticated() is False
    assert "Invalid MPIN" in str(auth.last_error)


def test_callback_oauth_error_parameter(client):
    """OAuth error parameter format handling."""
    response = client.get(
        "/smartapi/callback",
        params={
            "error": "access_denied",
            "error_description": "User cancelled authorization request",
        },
    )
    assert response.status_code == 400
    assert "Authentication Failed" in response.text
    auth = get_auth_state()
    assert auth.status == SmartApiAuthStatus.ERROR
    assert "User cancelled" in str(auth.last_error)


def test_secret_redaction(client):
    """Verify secrets are redacted in safe summaries, status endpoint, and string representations."""
    sample_secret = "SUPER_SECRET_JWT_KEY_12345"
    auth = get_auth_state()
    auth.set_authenticated(
        auth_token=sample_secret,
        feed_token="FEED_SECRET_9876",
        refresh_token="REFRESH_SECRET_5432",
        client_code="CLIENT007",
    )

    summary = auth.get_safe_summary()
    assert sample_secret not in str(summary)
    assert "FEED_SECRET_9876" not in str(summary)
    assert "REFRESH_SECRET_5432" not in str(summary)
    assert summary["has_auth_token"] is True
    assert summary["has_feed_token"] is True
    assert summary["client_code"] == "CL****07"

    # Representation must not contain secret
    repr_str = repr(auth)
    assert sample_secret not in repr_str

    # GET /smartapi/status must be redacted
    status_resp = client.get("/smartapi/status")
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert sample_secret not in str(status_data)
    assert status_data["authenticated"] is True
    assert status_data["execution_mode"] == "PAPER"


def test_execution_mode_remains_paper():
    """Requirement 6: EXECUTION_MODE must remain PAPER and live routing blocked."""
    router = ExecutionRouter()
    assert router.mode == ExecutionMode.PAPER
    assert router.is_live_enabled() is False

    # Attempting to configure LIVE mode forces back to PAPER due to Real-Money Gate lock
    live_router = ExecutionRouter(mode="LIVE")
    assert live_router.mode == ExecutionMode.PAPER
    assert live_router.is_live_enabled() is False


def test_callback_cannot_place_order():
    """Security verification: callback has no order route, SmartApiExecutor raises error."""
    class MockIntent:
        strategy_id = "S1_TREND_EXPANSION"
        direction = "LONG"

    # Paper executor succeeds in simulation
    router = get_execution_router()
    paper_res = router.route(MockIntent())
    assert paper_res["mode"] == "PAPER"
    assert paper_res["status"] == "PAPER_EXECUTED"

    # SmartApiExecutor hard-raises ExecutionSecurityError
    smart_exec = SmartApiExecutor()
    with pytest.raises(ExecutionSecurityError) as exc_info:
        smart_exec.execute(MockIntent())
    assert "CRITICAL SECURITY BLOCK" in str(exc_info.value)
    assert "Real-Money Gates in Phase 6 evaluated to FAIL" in str(exc_info.value)


def test_smartapi_auth_state_lifecycle():
    """Verify state transitions: NOT_AUTH -> AUTH -> ERROR -> clear -> NOT_AUTH."""
    auth = get_auth_state()
    assert auth.status == SmartApiAuthStatus.NOT_AUTHENTICATED
    assert auth.is_authenticated() is False

    # 1. Login success
    auth.set_authenticated(auth_token="token_1", client_code="XYZ123")
    assert auth.status == SmartApiAuthStatus.AUTHENTICATED
    assert auth.is_authenticated() is True

    # 2. Re-login error
    auth.set_error("Session expired")
    assert auth.status == SmartApiAuthStatus.ERROR
    assert auth.is_authenticated() is False

    # 3. Explicit clear
    auth.clear()
    assert auth.status == SmartApiAuthStatus.NOT_AUTHENTICATED
    assert auth.is_authenticated() is False
    assert auth.client_code is None


def test_login_url_generation(monkeypatch):
    """Verify login URL formatting with and without redirect URL."""
    monkeypatch.setattr(settings, "SMARTAPI_API_KEY", "test_api_key_123")
    monkeypatch.setattr(settings, "SMARTAPI_REDIRECT_URL", "https://tunnel.example.com/smartapi/callback")

    url = get_smartapi_login_url()
    assert "https://smartapi.angelone.in/publisher-login" in url
    assert "api_key=test_api_key_123" in url
    assert "redirect_url=https%3A%2F%2Ftunnel.example.com%2Fsmartapi%2Fcallback" in url


def test_smartapi_historical_client():
    """Verify SmartApiHistoricalClient returns empty DataFrame safely when unauthenticated."""
    from data_sources.smartapi_historical import SmartApiHistoricalClient
    client = SmartApiHistoricalClient(api_key="", client_code="")
    df = client.fetch_candles()
    assert df.empty

