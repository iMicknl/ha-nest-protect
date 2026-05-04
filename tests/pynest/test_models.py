"""Tests for pynest models."""

import datetime

import pytest

from custom_components.nest_protect.pynest.models import NestResponse


@pytest.fixture
def nest_response():
    """Create a sample NestResponse."""
    return NestResponse(
        access_token="test-token",
        email="test@example.com",
        expires_in="Sat, 03-May-2026 20:00:00 GMT",
        userid="user123",
        is_superuser=False,
        language="en",
        weave={"access_token": "weave-token"},
        user="user.123",
        is_staff=False,
    )


def test_nest_response_to_dict(nest_response):
    """Test serialization to dict."""
    result = nest_response.to_dict()
    assert result == {
        "access_token": "test-token",
        "email": "test@example.com",
        "expires_in": "Sat, 03-May-2026 20:00:00 GMT",
        "userid": "user123",
        "is_superuser": False,
        "language": "en",
        "weave": {"access_token": "weave-token"},
        "user": "user.123",
        "is_staff": False,
    }


def test_nest_response_from_dict():
    """Test deserialization from dict."""
    data = {
        "access_token": "test-token",
        "email": "test@example.com",
        "expires_in": "Sat, 03-May-2026 20:00:00 GMT",
        "userid": "user123",
        "is_superuser": False,
        "language": "en",
        "weave": {"access_token": "weave-token"},
        "user": "user.123",
        "is_staff": False,
    }
    result = NestResponse.from_dict(data)
    assert result.access_token == "test-token"
    assert result.email == "test@example.com"
    assert result.userid == "user123"


def test_nest_response_from_dict_returns_none_on_invalid():
    """Test from_dict returns None with missing fields."""
    assert NestResponse.from_dict({}) is None
    assert NestResponse.from_dict(None) is None


def test_nest_response_is_expired_with_buffer():
    """Test that is_expired respects the buffer parameter."""
    # Session expires in 4 minutes - within the 5 minute buffer
    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=4)
    expires_str = future.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT"

    response = NestResponse(
        access_token="t",
        email="e",
        expires_in=expires_str,
        userid="u",
        is_superuser=False,
        language="en",
        weave={},
        user="u",
        is_staff=False,
    )

    # Without buffer: not expired
    assert response.is_expired() is False
    # With 5 min buffer: expired
    assert response.is_expired(buffer_seconds=300) is True


def test_nest_response_is_expired_without_buffer():
    """Test that is_expired works without buffer (past expiry)."""
    past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
    expires_str = past.strftime("%a, %d-%b-%Y %H:%M:%S") + " GMT"

    response = NestResponse(
        access_token="t",
        email="e",
        expires_in=expires_str,
        userid="u",
        is_superuser=False,
        language="en",
        weave={},
        user="u",
        is_staff=False,
    )

    assert response.is_expired() is True
    assert response.is_expired(buffer_seconds=300) is True
