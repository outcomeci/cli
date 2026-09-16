# Tasks: Cloud Workflow Execution

- [x] T001 [US2] Add typed credential arguments and payload construction in `src/outcomeci/cli.py` and `src/outcomeci/cloud.py`
- [x] T002 [US2] Add secret-redaction and contract tests in `tests/test_vault.py`
- [x] T003 [US1] Add invocation-scoped workflow client models and requests in `src/outcomeci/cloud_runner/`
- [x] T004 [US1] Add `workflow` runner mode using the existing local executor in `src/outcomeci/cloud_runner/main.py`
- [x] T005 [US1] Add runner lease, recovery, and simulated Slack tests in `tests/`
- [x] T006 Document the typed Vault and cloud execution flow in `README.md`
- [x] T007 Use the Fargate container as the Codex isolation boundary for managed runs
- [x] T008 Inject API policy review into the credential-blind capability broker
- [x] T009 Materialize sanitized effect receipts and add one capability-free output repair pass
- [x] T010 Emit redacted artifact repair events and cover receipt isolation/repair behavior in tests
