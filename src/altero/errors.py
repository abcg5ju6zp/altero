"""Domain errors.

These carry no HTTP vocabulary. The API layer is responsible for mapping them
onto status codes, which keeps the service layer independent of the web
framework.
"""

from typing import Any


class AlteroError(Exception):
    """Base class for errors raised by the service layer."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AlteroError):
    """The requested object does not exist."""


class ForbiddenError(AlteroError):
    """The credential does not grant the required access."""


class InvalidInputError(AlteroError):
    """The request data is malformed or violates the schema."""


class PreconditionFailedError(AlteroError):
    """The library changed since the version the client last saw."""


class PreconditionRequiredError(AlteroError):
    """The request needs a version precondition it did not supply."""


class RequestTooLargeError(AlteroError):
    """More objects were submitted than one request may carry."""


class ExportAbortedError(AlteroError):
    """An export could not stay on its snapshot boundary and was aborted.

    Carries which library was being exported so the failure can be logged and
    reported precisely. Subclasses name the reason: the database snapshot was
    lost, or a boundary attachment disappeared, or one was replaced.
    """

    def __init__(
        self,
        message: str,
        *,
        library_type: str | None = None,
        owner_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.library_type = library_type
        self.owner_id = owner_id


class ExportConflictError(ExportAbortedError):
    """The library's snapshot could not be maintained while exporting."""


class AttachmentMissingError(ExportAbortedError):
    """A file that existed at the export boundary is gone before it was read."""

    def __init__(self, message: str, *, digest: str, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.digest = digest


class AttachmentChangedError(ExportAbortedError):
    """A boundary file was replaced between the boundary and the final check."""

    def __init__(self, message: str, *, digest: str, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.digest = digest


class ExportWriteError(AlteroError):
    """Writing the archive into the temporary workspace failed.

    A full disk or a device error while the bytes are going down: the archive
    never reached the final path, and the half-written workspace has been
    removed.
    """

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class ExportPublishError(AlteroError):
    """Moving the finished archive onto the final path failed.

    The validated archive could not be renamed into place (a device or
    permission error in `os.replace`); the final path is unchanged and the
    workspace has been removed.
    """

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class OAuthError(AlteroError):
    """An OAuth 2.0 protocol error, named by the code RFC 6749 §5.2 defines.

    The code is protocol vocabulary rather than HTTP vocabulary: an OAuth client
    branches on ``error``, not on the status carrying it, and the registry of
    codes belongs to the protocol. So it lives here with the other domain
    errors, and :mod:`altero.api.errors` still decides which status reports it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        #: One of the codes RFC 6749 §5.2 or RFC 6750 §3.1 defines --
        #: ``invalid_request``, ``invalid_client``, ``invalid_grant``,
        #: ``invalid_scope``, ``unauthorized_client``,
        #: ``unsupported_grant_type``, ``unsupported_response_type``.
        self.code = code
