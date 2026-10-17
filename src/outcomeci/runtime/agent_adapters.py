"""Execution command adapters for registered agent connectors.

These describe commands only. The runtime owns process execution, isolation,
capability grants, cancellation, artifacts and transcripts.
"""

from outcomeci_connectors.agents import agent_provider


def command_for(
    agent: str, model: str | None, prompt: str, *, container_isolated: bool, read_only: bool
) -> tuple[list[str], str | None]:
    agent_provider(agent)
    if agent == "codex":
        argv = ["codex", "exec", "--approve-for-me", "--skip-git-repo-check"]
        if container_isolated:
            # OutcomeCI Cloud runs inside a dedicated, least-privilege Fargate
            # task. Codex's nested Linux sandbox is unavailable there; the
            # container and capability broker are the security boundary.
            argv = [
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
            ]
        if read_only and not container_isolated:
            # Inside a container Codex's own sandbox is unavailable too; a
            # read-only call there runs in an empty temporary workspace with
            # no capability access, which is what keeps it read-only.
            argv = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check"]
        if model:
            argv += ["--model", model]
        argv += ["-"]
        input_text = prompt
    elif agent == "claude":
        argv = ["claude", "--print", "--permission-mode", "acceptEdits"]
        if container_isolated:
            # Same reasoning as Codex above: OutcomeCI Cloud's Fargate task is
            # the security boundary, and this runs fully non-interactively.
            # acceptEdits only auto-approves file edits, not arbitrary tool
            # calls -- the integration broker CLI still hits an approval
            # prompt that can never be answered here, and Claude reports the
            # call as rejected rather than actually invoking it.
            argv = ["claude", "--print", "--dangerously-skip-permissions"]
        if read_only:
            argv = ["claude", "--print", "--tools", "", "--permission-mode", "default"]
        if model:
            argv += ["--model", model]
        # On stdin, not argv: a single argument is capped at 128 KiB, and a
        # prompt carrying a step's context or a review's receipts exceeds it.
        input_text = prompt
    else:  # opencode, already validated above
        argv = ["opencode", "run", "--pure", "--auto", "--format", "json", "--model", model, prompt]
        input_text = None
    return argv, input_text
