"""Public API for Simple Harness Service SDK."""

from .auth import AuthenticatedContext, Capability, ContextAuthority
from .client import ConversationClient, ServicePort
from .contracts import (
    FRAME_MAX_BYTES,
    MESSAGE_MAX_BYTES,
    CancelRequest,
    CommandReceipt,
    CommandSnapshot,
    CommandState,
    ContinueRequest,
    GetRequest,
    HealthSnapshot,
    OutputState,
    ServiceError,
    ServiceErrorCode,
    StartRequest,
)
from .identity import IdentityProjector, Principal
from .service import HarnessAdapter, HarnessService
from .transports import UnixServiceClient, UnixServiceServer
from .version import __version__

__all__ = (
    "FRAME_MAX_BYTES",
    "MESSAGE_MAX_BYTES",
    "AuthenticatedContext",
    "CancelRequest",
    "Capability",
    "CommandReceipt",
    "CommandSnapshot",
    "CommandState",
    "ContextAuthority",
    "ContinueRequest",
    "ConversationClient",
    "GetRequest",
    "HarnessAdapter",
    "HarnessService",
    "HealthSnapshot",
    "IdentityProjector",
    "OutputState",
    "Principal",
    "ServiceError",
    "ServiceErrorCode",
    "ServicePort",
    "StartRequest",
    "UnixServiceClient",
    "UnixServiceServer",
    "__version__",
)
