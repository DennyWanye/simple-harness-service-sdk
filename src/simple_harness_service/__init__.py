"""Public API for Simple Harness Service SDK."""

from .auth import AuthenticatedContext, Capability, ContextAuthority
from .bom import load_bom, validate_installed_bom, validate_metadata_requirements
from .client import ConversationClient, ServicePort
from .contracts import (
    FRAME_MAX_BYTES,
    MESSAGE_MAX_BYTES,
    CancelRequest,
    CommandKind,
    CommandOutcome,
    CommandReceipt,
    CommandSnapshot,
    CommandState,
    ContinueRequest,
    GetRequest,
    HealthSnapshot,
    OutputState,
    RunState,
    ServiceError,
    ServiceErrorCode,
    StartRequest,
)
from .credentials import CredentialBundle, load_credentials, provision_credentials
from .identity import IdentityProjector, Principal
from .service import HarnessAdapter, HarnessService
from .terminal_ui import ChatUiConfig
from .transports import UnixServiceClient, UnixServiceServer
from .version import __version__

__all__ = (
    "FRAME_MAX_BYTES",
    "MESSAGE_MAX_BYTES",
    "AuthenticatedContext",
    "CancelRequest",
    "Capability",
    "ChatUiConfig",
    "CommandKind",
    "CommandOutcome",
    "CommandReceipt",
    "CommandSnapshot",
    "CommandState",
    "ContextAuthority",
    "ContinueRequest",
    "ConversationClient",
    "CredentialBundle",
    "GetRequest",
    "HarnessAdapter",
    "HarnessService",
    "HealthSnapshot",
    "IdentityProjector",
    "OutputState",
    "Principal",
    "RunState",
    "ServiceError",
    "ServiceErrorCode",
    "ServicePort",
    "StartRequest",
    "UnixServiceClient",
    "UnixServiceServer",
    "__version__",
    "load_bom",
    "load_credentials",
    "provision_credentials",
    "validate_installed_bom",
    "validate_metadata_requirements",
)
