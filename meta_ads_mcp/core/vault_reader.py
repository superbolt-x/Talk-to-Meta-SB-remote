"""
Vault gate stub — vault/marketing-vault folder is not used in this deployment.

enforce_vault_gate is kept as a no-op for backward compat with write modules.
"""
from typing import Optional


def enforce_vault_gate(account_id: str, corridor: str) -> tuple[Optional[dict], dict]:
    """No-op vault gate. Always allows the operation through with an empty context."""
    return (None, {})
