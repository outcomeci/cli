import json
from urllib.parse import parse_qs

import httpx
import pytest
import yaml
from outcomeci_connectors.providers.google.analytics import PROVIDER
from test_auth_kinds import rsa_key, typed, verified_claims

from outcomeci.broker import executor as integrations
from outcomeci.broker.executor import IntegrationExecutor
from outcomeci.broker.policy import PolicyExecutor
from outcomeci.runtime.process import ExecutionError
from outcomeci.workflow import language as v1
from outcomeci.workflow.compiler import compile_workflow


@pytest.mark.parametrize("kind", ["oauth2", "jwt_bearer"])
def test_google_reports_authenticate_and_enforce_property(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(integrations, "_safe_destination", lambda *args: None)
    monkeypatch.setattr(v1, "providers", lambda: {PROVIDER.name: PROVIDER})
    path = tmp_path / "analytics.outcome.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "outcomeci.workflow/v1",
                "trigger": "manual",
                "secrets": {"google": "vault:google/analytics"},
                "apis": {"analytics": {"uses": "google.analytics", "auth": "secrets.google"}},
                "steps": [
                    {
                        "report": {
                            "reason": "Summarize traffic",
                            "can": [{"analytics.report": {"property": "1234"}}],
                        }
                    }
                ],
            }
        )
    )
    compiled = compile_workflow(path)
    pem, public = rsa_key()
    credential = (
        typed(
            kind, {"client_secret": "secret", "refresh_token": "refresh"}, {"client_id": "client"}
        )
        if kind == "oauth2"
        else typed(kind, {"private_key": pem}, {"issuer": "test@project.iam.gserviceaccount.com"})
    )
    seen = []

    def server(request):
        seen.append(request)
        if request.url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            if kind == "oauth2":
                assert form["refresh_token"] == ["refresh"]
                assert form["client_id"] == ["client"]
            else:
                claims = verified_claims(form["assertion"][0], public)
                assert claims["iss"] == "test@project.iam.gserviceaccount.com"
                assert claims["aud"] == "https://oauth2.googleapis.com/token"
                assert claims["scope"] == "https://www.googleapis.com/auth/analytics.readonly"
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        assert (
            str(request.url)
            == "https://analyticsdata.googleapis.com/v1beta/properties/1234:runReport"
        )
        assert request.headers["Authorization"] == "Bearer access"
        assert "property" not in json.loads(request.content)
        return httpx.Response(200, json={"rows": [], "rowCount": 0})

    executor = IntegrationExecutor(
        compiled,
        resolver=lambda _: credential,
        transport=httpx.MockTransport(server),
        reviewed=True,
    )
    inputs = {
        "report": {
            "metrics": [{"name": "activeUsers"}],
            "dateRanges": [{"startDate": "7daysAgo", "endDate": "yesterday"}],
            "limit": "100",
        }
    }
    policy = PolicyExecutor(
        executor,
        tmp_path,
        {},
        grants=[{"capability": "analytics.report", "args": {"property": "1234"}, "as": None}],
    )
    scoped = policy._apply_grants("analytics.report", inputs)[0]
    executor.execute("analytics.report", scoped, step="report")
    assert len(seen) == 2
    with pytest.raises(ExecutionError):
        policy._apply_grants("analytics.report", {**inputs, "property": "9999"})
    assert len(seen) == 2
