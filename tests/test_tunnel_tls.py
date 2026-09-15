import tomllib

import pytest

from outcomeci.process import ExecutionError
from outcomeci.tunnels import configuration


def grant():
    return {
        "hostname": "ws-test.tunnel.staging.outcomeci.com",
        "credential": "c" * 48,
        "server_host": "connect.tunnel.staging.outcomeci.com",
        "server_port": 443,
        "local_port": 3000,
        "transport": "wss",
    }


def test_public_transport_requires_explicit_trust_roots(tmp_path):
    with pytest.raises(ExecutionError):
        configuration(grant(), 3000)
    ca = tmp_path / "roots.pem"
    ca.write_text("test fixture")
    config = tomllib.loads(configuration(grant(), 3000, ca_file=ca))
    assert config["transport"]["protocol"] == "wss"
    assert config["transport"]["tls"]["serverName"] == grant()["server_host"]
    assert config["transport"]["tls"]["trustedCaFile"] == str(ca)
    assert config["serverPort"] == 443


def test_public_transport_cannot_downgrade_or_inject_config(tmp_path):
    ca = tmp_path / "roots.pem"
    ca.write_text("test fixture")
    for modified in (
        {"transport": "tcp"},
        {"transport": "websocket"},
        {"server_port": 80},
        {"server_host": "connect.example.com\nlog.to=/tmp/leak"},
    ):
        with pytest.raises(ExecutionError):
            configuration({**grant(), **modified}, 3000, ca_file=ca)
