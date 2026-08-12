"""API Management AI-gateway routing for the triage agent's model calls.

Mirrors the research agent's contract. The two agents intentionally do not share
an importable module: code deployment zips only this service directory, so any
sibling package would be missing at runtime. See docs/implementation-plan.md AD-1.

Only an opaque platform identity is forwarded. No prompt, completion, display
name or mail address ever travels in these headers.
"""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Final

from agent_framework.openai import OpenAIChatClient
from azure.ai.agentserver.core import get_request_context
from azure.core.credentials_async import AsyncTokenCredential
from openai import AsyncOpenAI

AGENT_NAME: Final[str] = "triage-agent"
UNATTRIBUTED: Final[str] = "unattributed"

_MODEL_SCOPE: Final[str] = "https://cognitiveservices.azure.com/.default"

# Attribution keys become Azure Monitor metric dimensions, so they are bounded in
# both charset and length. Unvalidated values would allow header injection and
# unbounded dimension cardinality, which silently breaks metric ingestion.
_USER_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9._:-]{1,64}\Z")


def create_gateway_chat_client(
    *,
    base_url: str,
    model: str,
    timeout_seconds: float,
) -> OpenAIChatClient:
    """Build a Responses chat client bound to the API Management gateway.

    The underlying SDK client is constructed here rather than letting the
    framework derive one, so authorization stays explicit: ``api_key`` is a
    placeholder and every request overrides ``Authorization`` with a freshly
    resolved Entra token. ``max_retries=0`` keeps a non-idempotent turn from
    being billed twice after an ambiguous transport failure.
    """
    return OpenAIChatClient(
        model=model,
        async_client=AsyncOpenAI(
            base_url=base_url,
            api_key="entra-id",
            max_retries=0,
            timeout=timeout_seconds,
        ),
        function_invocation_configuration={
            "max_iterations": 2,
            "max_function_calls": 1,
            "terminate_on_unknown_calls": True,
        },
    )


def normalize_user_key(raw: object) -> str:
    """Return a bounded, safe attribution key for a platform user identity."""
    if not isinstance(raw, str) or not raw:
        return UNATTRIBUTED
    if _USER_KEY_PATTERN.match(raw):
        return raw
    # An identity that is oversized or outside the accepted charset is hashed
    # rather than dropped, so the request stays attributable without widening
    # what can reach a metric dimension.
    return sha256(raw.encode("utf-8")).hexdigest()[:32]


def current_user_key() -> str:
    """Return the platform-supplied per-user identity for the current request.

    Foundry populates this from its own ``x-agent-user-id`` header, so it is a
    trusted, cross-agent identity rather than anything the caller chose. Outside
    a hosted request the context is empty and usage is reported as unattributed.
    """
    return normalize_user_key(get_request_context().user_id)


async def gateway_headers(credential: AsyncTokenCredential) -> dict[str, str]:
    """Build per-request authorization and attribution headers for the gateway.

    The credential caches and refreshes tokens internally, so resolving one per
    request is cheap and always yields an unexpired token.
    """
    token = await credential.get_token(_MODEL_SCOPE)
    return {
        "Authorization": f"Bearer {token.token}",
        "x-user-key": current_user_key(),
        "x-agent-name": AGENT_NAME,
    }
