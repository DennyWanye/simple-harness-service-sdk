"""Future AgentOS fixture: verified token subjects map to the same service principal."""

from __future__ import annotations

from collections.abc import Mapping

from ..auth import AuthenticatedContext, ContextAuthority
from ..contracts import ServiceError, ServiceErrorCode
from ..identity import Principal


class TokenSubjectAdapter:
    """Consumes a subject already authenticated by a trusted TLS/token adapter."""

    def __init__(
        self, authority: ContextAuthority, subjects: Mapping[str, Principal]
    ) -> None:
        self._authority = authority
        self._subjects = dict(subjects)

    def authenticate(
        self, verified_subject: str, *, observed_tls_binding: str, now: float
    ) -> AuthenticatedContext:
        try:
            principal = self._subjects[verified_subject]
        except KeyError as error:
            raise ServiceError(ServiceErrorCode.UNAUTHENTICATED) from error
        token = self._authority.issue(
            principal, channel_binding=observed_tls_binding, now=now
        ).token
        return self._authority.verify(
            token, observed_channel_binding=observed_tls_binding, now=now
        )

