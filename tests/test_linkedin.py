"""LinkedIn discovery and OAuth refresh, without inventing API operations."""

from urllib.parse import parse_qs

import httpx
import pytest
from outcomeci_connectors.providers.linkedin import PROVIDER, SCOPES
from test_auth_kinds import typed

from outcomeci.auth import Authenticator, AuthError
from outcomeci.v1 import providers


def auth():
    return {
        "connector": "linkedin",
        "credential": "vault:linkedin/account",
        **PROVIDER.contract()["auth"],
    }


def credential(scopes):
    return typed(
        "oauth2",
        {"client_secret": "secret", "refresh_token": "refresh"},
        {"client_id": "app", "grant_type": "refresh_token", "scopes": scopes},
    )


def test_linkedin_discovery():
    assert providers()["linkedin"].contract() == PROVIDER.contract()


@pytest.mark.parametrize(
    "scopes", [["w_member_social"], list(SCOPES), "w_member_social r_basicprofile"]
)
def test_linkedin_refresh_uses_only_selected_scopes_and_caches_token(scopes):
    requests = []

    def server(request):
        requests.append(request)
        assert str(request.url) == "https://www.linkedin.com/oauth/v2/accessToken"
        expected = scopes.split() if isinstance(scopes, str) else scopes
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["refresh_token"],
            "refresh_token": ["refresh"],
            "client_id": ["app"],
            "client_secret": ["secret"],
            "scope": [" ".join(expected)],
        }
        assert "Authorization" not in request.headers
        # LinkedIn omits token_type and need not rotate the refresh token.
        return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})

    broker = Authenticator()
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        for _ in range(2):
            headers = {}
            sensitive = broker.apply(client, auth(), credential(scopes), headers, {})
            assert headers == {"Authorization": "Bearer access"}
            assert "access" in sensitive
            assert "secret" in sensitive
    assert len(requests) == 1


@pytest.mark.parametrize("scopes", [None, [], "", ["unknown"], ["w_member_social"] * 2, [42], 42])
def test_invalid_scopes_never_reach_linkedin(scopes):
    def server(request):
        pytest.fail("Invalid scope selection must fail before sending a request")

    with (
        httpx.Client(transport=httpx.MockTransport(server)) as client,
        pytest.raises(AuthError) as caught,
    ):
        Authenticator().apply(client, auth(), credential(scopes), {}, {})
    assert caught.value.code == "integration.invalid_scopes"


def test_fixed_required_scopes_are_preserved_with_optional_scopes():
    declaration = auth()
    declaration["accepts"][0]["scopes"] = ["required"]
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: pytest.fail("No request expected"))
        ) as client,
        pytest.raises(AuthError),
    ):
        Authenticator().apply(client, declaration, credential(["w_member_social"]), {}, {})
