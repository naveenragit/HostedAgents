"""Normalize Responses conversation input without changing message roles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, Protocol, TypedDict, cast

MessageRole = Literal["user", "assistant"]

_ALLOWED_ROLES = frozenset({"user", "assistant"})
_MESSAGE_TYPES = frozenset({"message", "output_message"})
_TEXT_PART_TYPES = frozenset({"input_text", "output_text", "text"})


class NormalizedInputItem(TypedDict):
    """A role-preserving text item accepted by the downstream Responses API."""

    role: MessageRole
    content: str


class ConversationInputContext(Protocol):
    """Minimal Foundry context surface used by input normalization."""

    async def get_history(self) -> Sequence[object]: ...

    async def get_input_items(
        self,
        *,
        resolve_references: bool = True,
    ) -> Sequence[object]: ...


class InputValidationError(ValueError):
    """Raised when request input cannot be forwarded without changing meaning."""


class InputTooLargeError(InputValidationError):
    """Raised when total message content exceeds the configured boundary."""


def normalize_input_items(items: Sequence[object], max_chars: int) -> list[NormalizedInputItem]:
    """Return ordered user/assistant text items and enforce an aggregate limit."""
    normalized = [_normalize_item(item) for item in items]
    _enforce_limit(normalized, max_chars)
    return normalized


async def normalize_context_input(
    context: ConversationInputContext,
    max_chars: int,
) -> list[NormalizedInputItem]:
    """Read Foundry history and current input as separate channels, then merge them."""
    return normalize_conversation_items(
        await context.get_history(),
        await context.get_input_items(resolve_references=False),
        max_chars,
    )


def normalize_conversation_items(
    history_items: Sequence[object],
    input_items: Sequence[object],
    max_chars: int,
) -> list[NormalizedInputItem]:
    """Keep the newest complete Foundry history pairs before current input."""
    current = normalize_input_items(input_items, max_chars)
    history = [
        _normalize_item(item) for item in history_items if _field(item, "type") in _MESSAGE_TYPES
    ]
    remaining_chars = max_chars - sum(len(item["content"]) for item in current)

    selected_pairs: list[tuple[NormalizedInputItem, NormalizedInputItem]] = []
    used_chars = 0
    pair_end = len(history)
    if pair_end > 0 and history[pair_end - 1]["role"] == "user":
        pair_end -= 1

    while pair_end >= 2:
        user_item = history[pair_end - 2]
        assistant_item = history[pair_end - 1]
        if user_item["role"] != "user" or assistant_item["role"] != "assistant":
            break
        pair_chars = len(user_item["content"]) + len(assistant_item["content"])
        if used_chars + pair_chars > remaining_chars:
            break
        selected_pairs.append((user_item, assistant_item))
        used_chars += pair_chars
        pair_end -= 2

    selected_history = [item for pair in reversed(selected_pairs) for item in pair]
    return [*selected_history, *current]


def _normalize_item(item: object) -> NormalizedInputItem:
    item_type = _field(item, "type")
    if item_type not in _MESSAGE_TYPES:
        raise InputValidationError(f"Unsupported input item type: {item_type!r}.")

    role = _field(item, "role")
    if role not in _ALLOWED_ROLES:
        raise InputValidationError(f"Unsupported input message role: {role!r}.")

    return NormalizedInputItem(
        role=cast(MessageRole, role),
        content=_text_content(_field(item, "content")),
    )


def _enforce_limit(items: Sequence[NormalizedInputItem], max_chars: int) -> None:
    if sum(len(item["content"]) for item in items) > max_chars:
        raise InputTooLargeError(
            f"Request content exceeds the configured {max_chars}-character limit."
        )


def _text_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray)):
        raise InputValidationError("Message content must be text or a sequence of text parts.")

    texts: list[str] = []
    for part in content:
        part_type = _field(part, "type")
        text = _field(part, "text")
        if part_type not in _TEXT_PART_TYPES or not isinstance(text, str):
            raise InputValidationError(f"Unsupported message content type: {part_type!r}.")
        texts.append(text)
    return "".join(texts)


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)
