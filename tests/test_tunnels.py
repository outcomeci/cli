import tomllib

import pytest

from outcomeci.cli import parser
from outcomeci.process import ExecutionError
from outcomeci.tunnels import configuration, start, validate_target


@pytest.mark.parametrize(
    "target",
    [
        "https://127.0.0.1:3000",
        "http://example.com:3000",
        "http://u:p@127.0.0.1:3000",
        "http://127.0.0.1:3000/path",
        "http://127.0.0.1:3000?q=x",
        "http://127.0.0.1:80",
        "http://127.0.0.1:3000#x",
    ],
)
def test_unapproved_targets_rejected(target):
    with pytest.raises(ExecutionError):
        validate_target(target)


def test_explicit_loopback_target():
    assert validate_target("http://localhost:3000") == 3000


def test_configuration_is_scoped_and_injection_safe():
    grant = {
        "hostname": "ws-a.local",
        "credential": "c" * 48,
        "server_host": "127.0.0.1",
        "server_port": 7400,
        "local_port": 3000,
        "transport": "tcp",
    }
    data = tomllib.loads(configuration(grant, 3000))
    assert data["proxies"][0]["customDomains"] == ["ws-a.local"]
    assert data["proxies"][0]["localIP"] == "127.0.0.1"
    assert data["metadatas"]["credential"] == "c" * 48
    with pytest.raises(ExecutionError):
        configuration({**grant, "server_host": "remote"}, 3000)
    with pytest.raises(ExecutionError):
        configuration(grant, 4000)


def test_public_exposure_requires_acknowledgment():
    with pytest.raises(ExecutionError, match="--public"):
        start("workspace_a", "http://127.0.0.1:3000")


def test_cli_surface():
    args = parser().parse_args(
        ["tunnel", "start", "--workspace-id", "a", "--target", "http://127.0.0.1:3000", "--public"]
    )
    assert args.public and args.ttl_seconds == 900
