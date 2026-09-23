"""OutcomeCI's own regression harness: declarative, persona-based ecosystem
durability proofs. Ships in the same package as the product it tests — see
docs-quickstart-v1 and vault-credentials-v1 for why — but lives in its own
subpackage so it reads as self-testing infrastructure, not product code.
"""

from .simulation import bundled_definition, load_definition, run, verify_ledger

__all__ = ["bundled_definition", "load_definition", "run", "verify_ledger"]
