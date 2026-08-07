"""Construct the Azure credential selected for the current hosting environment."""

from __future__ import annotations

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

from app.config import CredentialMode, Settings

AzureCredential = DefaultAzureCredential | ManagedIdentityCredential


def build_azure_credential(settings: Settings) -> AzureCredential:
    """Use developer-chain auth locally and managed identity on deployed compute."""
    if settings.credential_mode is CredentialMode.MANAGED_IDENTITY:
        return ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
    return DefaultAzureCredential(exclude_managed_identity_credential=True)
