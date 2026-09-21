from outcomeci.cloud_runner.redaction import redact, redact_diagnostic


def test_redact_masks_bearer_tokens_in_free_text():
    assert redact("failed: Bearer abc.def-123 rejected") == ("failed: Bearer [REDACTED] rejected")


def test_redact_masks_api_key_shaped_tokens_outside_headers():
    assert redact("using sk-abc12345supersecret in the request") == (
        "using [REDACTED] in the request"
    )
    assert redact("token ghp_abc12345supersecret leaked") == "token [REDACTED] leaked"
    assert redact("token ghr_abc12345supersecret leaked") == "token [REDACTED] leaked"


def test_redact_masks_jwts():
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJhIn0.c2lnbmF0dXJl"
    assert redact(f"assertion={jwt}") == "assertion=[REDACTED]"


def test_redact_masks_pem_private_key_blocks():
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC\n"
        "-----END PRIVATE KEY-----"
    )
    assert redact(f"could not load key: {pem}") == ("could not load key: [REDACTED PEM]")


def test_redact_masks_sensitive_dict_keys_regardless_of_value_shape():
    assert redact({"Authorization": "anything", "note": "fine"}) == {
        "Authorization": "[REDACTED]",
        "note": "fine",
    }


def test_redact_diagnostic_truncates_long_messages():
    error = ValueError("x" * 5000)

    result = redact_diagnostic(error, max_length=100)

    assert len(result) == 101
    assert result.endswith("…")


def test_redact_diagnostic_scrubs_the_exception_text():
    error = ValueError("Bearer sk-abc12345supersecret was rejected")

    result = redact_diagnostic(error)

    assert "sk-abc12345supersecret" not in result
    assert "was rejected" in result
