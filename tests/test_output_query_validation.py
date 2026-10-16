import pytest
from test_output_history_runtime import workflow

from outcomeci.workflow.compiler import ConfigError


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(outputs=[]),
        lambda d: d["outputs"].update(sources=[]),
        lambda d: d["outputs"]["sources"]["past"].update(steps=[{}]),
        lambda d: d["outputs"]["sources"]["past"].update(steps=["assess", "assess"]),
        lambda d: d["outputs"]["sources"]["past"].update(fields=[{}]),
        lambda d: d["outputs"]["sources"]["past"].update(run_status=[]),
        lambda d: d["outputs"]["queries"]["previous"].update(source={}),
        lambda d: d["outputs"]["queries"]["previous"].update(where={"customer": {}}),
        lambda d: d["outputs"]["queries"]["previous"].update(where={"customer": {"eq": None}}),
        lambda d: d["outputs"]["queries"]["previous"].update(
            where={"customer": {"eq": float("nan")}}
        ),
        lambda d: d["outputs"]["queries"]["previous"].update(
            where={"customer": {"eq": {"ref": "trigger."}}}
        ),
        lambda d: d["outputs"]["queries"]["previous"].update(
            order_by=[{"field": {}, "direction": "asc"}]
        ),
        lambda d: d["outputs"]["queries"]["previous"].update(
            order_by=[{"field": "customer", "direction": []}]
        ),
        lambda d: d["outputs"]["queries"]["previous"].update(workflow_revision=[]),
        lambda d: d["outputs"]["queries"]["previous"].update(limit=True),
        lambda d: d["steps"][0]["assess"].update(query=[{}]),
        lambda d: d["steps"][0]["assess"].update(index={"fields": ["customer", "customer"]}),
    ],
)
def test_malformed_history_declarations_fail_compilation(tmp_path, change):
    with pytest.raises(ConfigError):
        workflow(tmp_path, bad=change)


def test_query_complexity_is_bounded_before_execution(tmp_path):
    with pytest.raises(ConfigError, match="order_by"):
        workflow(
            tmp_path,
            bad=lambda d: d["outputs"]["queries"]["previous"].update(
                order_by=[{"field": "customer", "direction": "asc"}] * 11
            ),
        )
