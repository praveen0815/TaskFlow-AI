from collections.abc import Mapping
from typing import Any, Protocol


class IntegrationAdapter(Protocol):
    """Contract for future authenticated, audited external integrations."""

    name: str

    async def execute(self, operation: str, arguments: Mapping[str, Any]) -> Any: ...


class EmailAdapter(IntegrationAdapter, Protocol):
    pass


class MessagingAdapter(IntegrationAdapter, Protocol):
    pass


class CRMAdapter(IntegrationAdapter, Protocol):
    pass


class WebhookAdapter(IntegrationAdapter, Protocol):
    pass


class ExternalAPIAdapter(IntegrationAdapter, Protocol):
    pass


class DocumentStorageAdapter(IntegrationAdapter, Protocol):
    pass
