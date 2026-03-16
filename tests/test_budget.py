"""
tests/test_budget.py — Tests for SetBudgetRequest Pydantic model and the
PUT /api/projects/{project_id}/budget endpoint.

Covers task_002/fix-2:
- Explicit type-guard validator (mode="before") rejects booleans and strings
- ConfigDict(strict=True) rejects implicit type coercion
- Field(gt=0, le=10_000) rejects zero, negative, and out-of-bounds values
- @field_validator("budget_usd") rejects NaN and Inf
- All invalid inputs return RFC 7807 Problem Detail (422) via FastAPI validation

Naming convention: test_<what>_when_<condition>_should_<expected>
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Helpers (reused from test_api.py pattern)
# ---------------------------------------------------------------------------


def _make_session_mgr():
    smgr = AsyncMock()
    smgr.is_healthy = AsyncMock(return_value=True)
    smgr.list_projects = AsyncMock(return_value=[])
    smgr.load_project = AsyncMock(return_value=None)
    smgr.set_project_budget = AsyncMock()
    return smgr


def _make_app(session_mgr=None):
    import state

    state.session_mgr = session_mgr if session_mgr is not None else _make_session_mgr()
    state.sdk_client = MagicMock()
    from dashboard.api import create_app

    return create_app()


# ---------------------------------------------------------------------------
# Unit tests: SetBudgetRequest Pydantic model validation
# ---------------------------------------------------------------------------


class TestSetBudgetRequestModel:
    """Unit tests for the SetBudgetRequest Pydantic model (task_002/fix-2)."""

    def test_set_budget_request_when_valid_float_should_accept(self):
        """A plain positive float within bounds is accepted."""
        from dashboard.api import SetBudgetRequest

        req = SetBudgetRequest(budget_usd=50.0)
        assert req.budget_usd == pytest.approx(50.0)

    def test_set_budget_request_when_valid_integer_should_accept(self):
        """An integer (non-bool) is accepted and coerced to float."""
        from dashboard.api import SetBudgetRequest

        req = SetBudgetRequest(budget_usd=100)
        assert req.budget_usd == pytest.approx(100.0)

    def test_set_budget_request_when_minimum_valid_value_should_accept(self):
        """Smallest positive float that passes gt=0 constraint."""
        from dashboard.api import SetBudgetRequest

        req = SetBudgetRequest(budget_usd=0.000001)
        assert req.budget_usd > 0

    def test_set_budget_request_when_maximum_valid_value_should_accept(self):
        """Upper bound (10,000) is accepted."""
        from dashboard.api import SetBudgetRequest

        req = SetBudgetRequest(budget_usd=10_000.0)
        assert req.budget_usd == pytest.approx(10_000.0)

    # --- Type-guard: booleans rejected ---

    def test_set_budget_request_when_bool_true_should_raise_validation_error(self):
        """True (bool subclass of int) must be rejected by the mode='before' validator."""
        from dashboard.api import SetBudgetRequest

        with pytest.raises(ValidationError) as exc_info:
            SetBudgetRequest(budget_usd=True)
        errors = exc_info.value.errors()
        assert any("boolean" in str(e).lower() for e in errors)

    def test_set_budget_request_when_bool_false_should_raise_validation_error(self):
        """False (bool subclass of int) must be rejected."""
        from dashboard.api import SetBudgetRequest

        with pytest.raises(ValidationError):
            SetBudgetRequest(budget_usd=False)

    # --- Type-guard: strings rejected (strict=True) ---

    def test_set_budget_request_when_string_number_should_raise_validation_error(self):
        """A numeric string like '100.0' must be rejected (strict=True prevents coercion)."""
        from dashboard.api import SetBudgetRequest

        with pytest.raises(ValidationError):
            SetBudgetRequest(budget_usd="100.0")

    def test_set_budget_request_when_empty_string_should_raise_validation_error(self):
        """An empty string is rejected."""
        from dashboard.api import SetBudgetRequest

        with pytest.raises(ValidationError):
            SetBudgetRequest(budget_usd="")

    def test_set_budget_request_when_none_should_raise_validation_error(self):
        """None is not a valid budget."""
        from dashboard.api import SetBudgetRequest

        with pytest.raises(ValidationError):
            SetBudgetRequest(budget_usd=None)

    # --- Range validation: zero and negative rejected ---

    def test_set_budget_request_when_zero_should_raise_validation_error(self):
        """Zero is rejected by gt=0 constraint."""
        from dashboard.api import SetBudgetRequest

        with pytest.raises(ValidationError):
            SetBudgetRequest(budget_usd=0.0)

    def test_set_budget_request_when_negative_should_raise_validation_error(self):
        """Negative values are rejected by gt=0 constraint."""
        from dashboard.api import SetBudgetRequest

        with pytest.raises(ValidationError):
            SetBudgetRequest(budget_usd=-1.0)

    def test_set_budget_request_when_exceeds_max_should_raise_validation_error(self):
        """Values > 10,000 are rejected by le=10_000 constraint."""
        from dashboard.api import SetBudgetRequest

        with pytest.raises(ValidationError):
            SetBudgetRequest(budget_usd=10_000.01)

    def test_set_budget_request_when_very_large_should_raise_validation_error(self):
        """Very large numeric values are rejected."""
        from dashboard.api import SetBudgetRequest

        with pytest.raises(ValidationError):
            SetBudgetRequest(budget_usd=1_000_000.0)

    # --- Finiteness validation: NaN and Inf rejected ---

    def test_set_budget_request_when_nan_should_raise_validation_error(self):
        """NaN is rejected by the finiteness validator."""
        from dashboard.api import SetBudgetRequest

        with pytest.raises(ValidationError) as exc_info:
            SetBudgetRequest(budget_usd=float("nan"))
        errors = exc_info.value.errors()
        assert any("finite" in str(e).lower() or "nan" in str(e).lower() for e in errors)

    def test_set_budget_request_when_positive_inf_should_raise_validation_error(self):
        """Positive infinity is rejected by the finiteness validator."""
        from dashboard.api import SetBudgetRequest

        with pytest.raises(ValidationError):
            SetBudgetRequest(budget_usd=float("inf"))

    def test_set_budget_request_when_negative_inf_should_raise_validation_error(self):
        """Negative infinity is rejected (fails gt=0 and finiteness check)."""
        from dashboard.api import SetBudgetRequest

        with pytest.raises(ValidationError):
            SetBudgetRequest(budget_usd=float("-inf"))

    # --- Normalisation: precision rounded to 6 decimal places ---

    def test_set_budget_request_when_many_decimals_should_round_to_6_places(self):
        """Valid values with many decimal places are normalised to 6 places."""
        from dashboard.api import SetBudgetRequest

        req = SetBudgetRequest(budget_usd=1.123456789)
        assert req.budget_usd == round(1.123456789, 6)


# ---------------------------------------------------------------------------
# Integration tests: PUT /api/projects/{project_id}/budget HTTP endpoint
# ---------------------------------------------------------------------------


class TestBudgetEndpoint:
    """Integration tests for PUT /api/projects/{project_id}/budget endpoint."""

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_valid_budget_should_return_200(self):
        """A valid numeric budget returns 200 with ok=True."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": 100.0},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["budget_usd"] == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_valid_budget_should_call_set_project_budget(self):
        """A valid budget causes session_mgr.set_project_budget() to be called."""
        smgr = _make_session_mgr()
        app = _make_app(smgr)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": 50.0},
            )
        smgr.set_project_budget.assert_awaited_once_with("my-project", pytest.approx(50.0))

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_boolean_true_should_return_4xx(self):
        """Sending True as budget_usd must be rejected (RFC 7807 error response)."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": True},
            )
        # validation_exception_handler converts 422 → 400; either is a client error
        assert resp.status_code in (400, 422)
        body = resp.json()
        # RFC 7807 Problem Detail format
        assert "type" in body
        assert "status" in body
        assert "detail" in body

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_boolean_false_should_return_4xx(self):
        """Sending False as budget_usd is rejected."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": False},
            )
        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_string_budget_should_return_4xx(self):
        """Sending a string as budget_usd must be rejected (strict mode)."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": "9999.99"},
            )
        # validation_exception_handler converts 422 → 400
        assert resp.status_code in (400, 422)
        body = resp.json()
        assert "type" in body
        assert body["type"] == "about:blank"

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_zero_should_return_4xx(self):
        """Zero budget is rejected (gt=0 constraint)."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": 0},
            )
        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_negative_budget_should_return_4xx(self):
        """Negative budget values are rejected."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": -100.0},
            )
        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_exceeds_max_should_return_4xx(self):
        """Budget exceeding 10,000 is rejected (le=10_000 constraint)."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": 10_000.01},
            )
        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_nan_should_return_422(self):
        """NaN is rejected; JSON itself doesn't support NaN so the body may be malformed."""
        # JSON standard doesn't allow NaN; clients sending it via JSON will get 422
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                content=b'{"budget_usd": NaN}',
                headers={"Content-Type": "application/json"},
            )
        # Should be 400 (malformed JSON) or 422 (validation failure)
        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_missing_body_should_return_422(self):
        """Missing request body returns 422 or 400 in RFC 7807 format."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put("/api/projects/my-project/budget")
        assert resp.status_code in (400, 422)
        body = resp.json()
        assert "type" in body
        assert body["type"] == "about:blank"

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_missing_field_should_return_422(self):
        """Body without budget_usd field returns 422."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"wrong_field": 100.0},
            )
        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_no_db_should_return_500_rfc7807(self):
        """When session_mgr is None, endpoint returns RFC 7807 500."""
        import state

        state.session_mgr = None
        state.sdk_client = MagicMock()
        from dashboard.api import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": 100.0},
            )
        assert resp.status_code == 500
        body = resp.json()
        assert body["type"] == "about:blank"
        assert body["status"] == 500

    @pytest.mark.asyncio
    async def test_budget_endpoint_response_has_rfc7807_fields_on_error(self):
        """All error responses from budget endpoint have RFC 7807 required fields."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": "not-a-number"},
            )
        assert resp.status_code in (400, 422)
        body = resp.json()
        # RFC 7807 Problem Detail must have these four keys
        assert "type" in body
        assert "title" in body
        assert "status" in body
        assert "detail" in body

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_boundary_value_10000_should_return_200(self):
        """Exact upper boundary (10,000.0) is valid."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": 10000.0},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_list_payload_should_return_422(self):
        """A JSON array as budget_usd is rejected by type guard."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": [100.0]},
            )
        assert resp.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_budget_endpoint_when_dict_payload_should_return_422(self):
        """A JSON object as budget_usd is rejected by type guard."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": {"value": 100.0}},
            )
        assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# RFC 7807 error response contract
# ---------------------------------------------------------------------------


class TestBudgetErrorResponseContract:
    """Verify the RFC 7807 Problem Detail contract for all error scenarios."""

    @pytest.mark.asyncio
    async def test_4xx_error_response_has_required_rfc7807_fields(self):
        """Each validation error carries type/title/status/detail (RFC 7807)."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": True},
            )
        assert resp.status_code in (400, 422)
        body = resp.json()
        assert body.get("type") == "about:blank"
        assert "title" in body
        assert "status" in body
        assert isinstance(body.get("status"), int)
        assert "detail" in body

    @pytest.mark.asyncio
    async def test_error_status_field_matches_http_status_code(self):
        """The 'status' field inside the body matches the HTTP status code."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": "bad"},
            )
        assert resp.status_code in (400, 422)
        body = resp.json()
        # The RFC 7807 status field must match the HTTP status code
        assert body.get("status") == resp.status_code

    @pytest.mark.asyncio
    async def test_500_error_response_has_required_rfc7807_fields(self):
        """Server errors (500) from budget endpoint return RFC 7807 fields."""
        import state

        state.session_mgr = None
        state.sdk_client = MagicMock()
        from dashboard.api import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                "/api/projects/my-project/budget",
                json={"budget_usd": 100.0},
            )
        assert resp.status_code == 500
        body = resp.json()
        assert body.get("type") == "about:blank"
        assert "title" in body
        assert body.get("status") == 500
        assert "detail" in body
