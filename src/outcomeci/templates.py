"""Built-in templates: the `oci init` workflow and the prompt each v1 step runs with."""

V1_STEP_TASK = """{shared}

## Step: {step}

{instructions}

{environment} Write only this step's result file and notes beneath {outcome_root}. Call an API capability with `{runtime_cli} integration execute <capability> --phase {step} --input-stdin`, passing its input as JSON on stdin; each capability's input schema is in the context below. The capability broker holds the credentials and enforces this step's grants.

{context_json}"""

WORKFLOW_YAML = """# Edit this workflow, then run it in the runner container:
#
#   oci vault local init
#   oci vault local put github --value-stdin     # a GitHub token that can read the repo
#   oci workflow run --payload .outcomeci/request.json
#
# When it does what you want, publish it with `oci workflow sync`.
apiVersion: outcomeci.workflow/v1
name: default

trigger: manual

secrets:
  github: vault:github

apis:
  github: {uses: github, auth: secrets.github}

reasoning:
  default: {runner: codex}

steps:
  - investigate:
      reason: investigate.md
      from: trigger
      can:
        - github.read: {repo: trigger.repo}
      returns:
        findings: [{path: string, note: string}]

  - plan:
      reason: plan.md
      with: [trigger, investigate.findings]
      returns:
        plan: {summary, steps: [string]}
"""

WORKFLOW_INSTRUCTIONS = {
    "investigate.md": """# Investigate

Read the request in the trigger. Use the GitHub API to find the files in
`trigger.repo` the request touches, and return one finding per file: its path
and what about it matters for the request. Do not change anything.
""",
    "plan.md": """# Plan

Turn the request and the findings into a short plan: a one-paragraph summary
and the ordered steps a pull request would take. Name files by path.
""",
}

WORKFLOW_REQUEST = """{
  "request": "Add a /health endpoint that returns 200 when the service is up.",
  "repo": {"owner": "your-org", "name": "your-repo"}
}
"""
