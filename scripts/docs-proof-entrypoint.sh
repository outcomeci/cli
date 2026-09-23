#!/usr/bin/env bash
# Entrypoint for the docs-proof-runner image (Dockerfile.docs-proof-runner).
#
# Deliberately installs outcomeci-cli at container *start*, not at build time:
# the whole point of this proof is to test whatever a real user would get by
# installing the CLI right now in this environment, not a version baked into
# an image at some earlier build. See docs-quickstart-v1.proof.yml.
set -uo pipefail

: "${PROOF_ENVIRONMENT:?PROOF_ENVIRONMENT is required}"
: "${PROOF_INSTALL_SOURCE:?PROOF_INSTALL_SOURCE is required}"
: "${PROOF_NAME:?PROOF_NAME is required, e.g. docs-quickstart-v1 or vault-credentials-v1}"

if [ "$PROOF_NAME" = "docs-quickstart-v1" ]; then
  : "${OUTCOMECI_DOCS_BASE_URL:?OUTCOMECI_DOCS_BASE_URL is required for docs-quickstart-v1}"
fi

case "$PROOF_INSTALL_SOURCE" in
  codeartifact)
    : "${CODEARTIFACT_DOMAIN:?}" "${CODEARTIFACT_DOMAIN_OWNER:?}" "${CODEARTIFACT_REPOSITORY:?}"
    aws codeartifact login --tool pip \
      --domain "$CODEARTIFACT_DOMAIN" \
      --domain-owner "$CODEARTIFACT_DOMAIN_OWNER" \
      --repository "$CODEARTIFACT_REPOSITORY"
    ;;
  pypi)
    # No CodeArtifact hop: the real public PyPI package, exactly what a
    # customer gets from the quickstart's `pipx install outcomeci-cli`.
    ;;
  *)
    echo "unknown PROOF_INSTALL_SOURCE: $PROOF_INSTALL_SOURCE" >&2
    exit 1
    ;;
esac

python -m pip install --user --no-cache-dir --upgrade outcomeci-cli
export PATH="$HOME/.local/bin:$PATH"
CLI_VERSION=$(python -m pip show outcomeci-cli | awk '/^Version:/{print $2}')

echo "testing outcomeci-cli ${CLI_VERSION} proof=${PROOF_NAME} (${PROOF_ENVIRONMENT}, via ${PROOF_INSTALL_SOURCE})"

oci proof run --name "$PROOF_NAME" --workspace /proof --report /proof/report.json
EXIT_CODE=$?
PASSED=$([ "$EXIT_CODE" -eq 0 ] && echo 1 || echo 0)

# The alarm reads this metric, not the task's exit code: EventBridge Scheduler
# doesn't surface RunTask container exit codes as a CloudWatch metric on its
# own, so the container reports its own pass/fail. Dimensioned by which proof
# ran too, since one task now runs one of several proofs, each with its own
# alarm.
aws cloudwatch put-metric-data \
  --namespace "OutcomeCI/DocsProof" \
  --metric-name ProofPassed \
  --dimensions "Environment=${PROOF_ENVIRONMENT},Proof=${PROOF_NAME}" \
  --value "$PASSED"

# A second, version-dimensioned metric — deliberately separate from the one
# above, so adding it can never change what the environment-level alarm
# matches. This is what the release promote workflow checks: it will not
# promote a version staging hasn't run every gating proof against and passed.
aws cloudwatch put-metric-data \
  --namespace "OutcomeCI/DocsProof" \
  --metric-name ProofPassedByVersion \
  --dimensions "Environment=${PROOF_ENVIRONMENT},Proof=${PROOF_NAME},Version=${CLI_VERSION}" \
  --value "$PASSED"

exit "$EXIT_CODE"
