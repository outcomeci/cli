from outcomeci.conformance import run


def test_built_in_runtime_contract_is_conformant() -> None:
    result = run()
    assert result["conformant"] is True
    assert all(result["checks"].values())
    assert result["contract"] == "outcomeci.runner/v1alpha1"
