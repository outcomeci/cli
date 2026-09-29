"""Typed Vault credentials: the same shape in the local Vault and OutcomeCI Vault.

A typed credential is `{credential_type, configuration, secrets}`. The
configuration and secret fields each type may carry match OutcomeCI Vault, so
a credential stored locally resolves exactly like a leased one. A connector
fixes its own endpoints and headers, so those configuration fields are
optional.
"""

from __future__ import annotations

import argparse
from typing import Any

from .process import ExecutionError

CREDENTIAL_TYPES = (
    "api_key",
    "auth_header",
    "basic",
    "oauth2",
    "oidc",
    "jwt_bearer",
    "app_installation",
)
CONFIGURATION_FIELDS: dict[str, tuple[str, ...]] = {
    "api_key": ("header_name", "prefix"),
    "auth_header": ("header_name", "scheme"),
    "basic": (),
    "oauth2": ("token_url", "client_id", "grant_type", "scopes", "audience", "account_id"),
    "oidc": ("issuer_url", "client_id", "scopes", "audience"),
    "jwt_bearer": ("token_url", "issuer", "audience", "scope", "algorithm", "subject"),
    "app_installation": ("app_id", "installation_id"),
}
SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "api_key": ("api_key",),
    "auth_header": ("value",),
    "basic": ("username", "password"),
    "oauth2": ("client_secret", "refresh_token"),
    "oidc": ("client_secret",),
    "jwt_bearer": ("private_key",),
    "app_installation": ("private_key",),
}
REQUIRED_CONFIGURATION: dict[str, tuple[str, ...]] = {
    "oauth2": ("client_id",),
    "oidc": ("client_id",),
    "jwt_bearer": ("issuer",),
    "app_installation": ("app_id", "installation_id"),
}
# The secret field a single --value or --value-stdin fills.
DEFAULT_SECRET = {
    "api_key": "api_key",
    "auth_header": "value",
    "oauth2": "client_secret",
    "oidc": "client_secret",
    "jwt_bearer": "private_key",
    "app_installation": "private_key",
}
# Command-line option, configuration field.
OPTIONS = (
    ("header_name", "header_name"),
    ("prefix", "prefix"),
    ("scheme", "scheme"),
    ("token_url", "token_url"),
    ("issuer_url", "issuer_url"),
    ("client_id", "client_id"),
    ("grant_type", "grant_type"),
    ("scope", "scopes"),
    ("audience", "audience"),
    ("account_id", "account_id"),
    ("issuer", "issuer"),
    ("subject", "subject"),
    ("app_id", "app_id"),
    ("installation_id", "installation_id"),
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The options that describe a typed credential, shared by both Vaults."""
    parser.add_argument(
        "--secrets-json-stdin",
        action="store_true",
        help="Read a JSON object of secret fields from stdin, such as username and password",
    )
    parser.add_argument(
        "--credential-type",
        choices=CREDENTIAL_TYPES,
        help="Store a typed credential; a connector authenticates with it by its type",
    )
    parser.add_argument("--header-name", help="Header an api_key or auth_header value goes in")
    parser.add_argument("--prefix", help="Prefix before an api_key value")
    parser.add_argument("--scheme", help="Scheme before an auth_header value, such as Bearer")
    parser.add_argument("--token-url", help="oauth2 or jwt_bearer token endpoint")
    parser.add_argument("--issuer-url", help="oidc issuer, for issuers that vary per account")
    parser.add_argument("--client-id", help="oauth2 or oidc client id")
    parser.add_argument(
        "--grant-type",
        choices=("client_credentials", "refresh_token"),
        help="oauth2 grant the runtime runs",
    )
    parser.add_argument(
        "--scope", action="append", default=[], help="oauth2 or oidc scope (repeatable)"
    )
    parser.add_argument("--audience", help="Token audience")
    parser.add_argument("--account-id", help="oauth2 account id, for providers that need one")
    parser.add_argument("--issuer", help="jwt_bearer issuer (iss claim)")
    parser.add_argument("--subject", help="jwt_bearer subject (sub claim)")
    parser.add_argument("--app-id", help="app_installation app id")
    parser.add_argument("--installation-id", help="app_installation installation id")
    parser.add_argument(
        "--secret-name",
        choices=sorted({name for names in SECRET_FIELDS.values() for name in names}),
        help="Secret field --value or --value-stdin fills; inferred for most types",
    )


def build(
    credential_type: str,
    args: argparse.Namespace,
    *,
    value: str | None,
    secrets: dict[str, str] | None,
) -> dict[str, Any]:
    """A typed credential from command-line options, checked like OutcomeCI Vault checks it."""
    configuration: dict[str, Any] = {}
    for option, name in OPTIONS:
        given = getattr(args, option, None)
        if given in (None, [], ""):
            continue
        if name not in CONFIGURATION_FIELDS[credential_type]:
            raise ExecutionError(
                f"--{option.replace('_', '-')} does not apply to a {credential_type} credential"
            )
        configuration[name] = given
    missing = [
        name
        for name in REQUIRED_CONFIGURATION.get(credential_type, ())
        if name not in configuration
    ]
    if missing:
        raise ExecutionError(
            f"a {credential_type} credential needs "
            + ", ".join(f"--{name.replace('_', '-')}" for name in missing)
        )
    if secrets is None:
        name = args.secret_name or DEFAULT_SECRET.get(credential_type)
        if name is None:
            raise ExecutionError(
                f"a {credential_type} credential takes --secrets-json-stdin with "
                + " and ".join(SECRET_FIELDS[credential_type])
            )
        secrets = {name: value or ""}
    unknown = set(secrets) - set(SECRET_FIELDS[credential_type])
    if unknown:
        raise ExecutionError(
            f"a {credential_type} credential does not hold {', '.join(sorted(unknown))}"
        )
    needed = list(SECRET_FIELDS[credential_type])
    if credential_type == "oauth2":
        needed = ["client_secret"] + (
            ["refresh_token"] if configuration.get("grant_type") == "refresh_token" else []
        )
    absent = [name for name in needed if not secrets.get(name)]
    if absent:
        raise ExecutionError(f"a {credential_type} credential needs {', '.join(absent)}")
    return {"credential_type": credential_type, "configuration": configuration, "secrets": secrets}
