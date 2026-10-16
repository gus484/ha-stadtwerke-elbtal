"""Exceptions raised by the portal client."""


class PortalError(Exception):
    """Base class for all portal-related errors."""


class AuthError(PortalError):
    """The portal rejected the credentials.

    Raised only by the login call itself. Anything that goes wrong after a
    login the portal accepted is some other ``PortalError`` — mapping it here
    would send Home Assistant into the reauth dialog and invite repeated logins,
    which is how utility portals lock accounts.
    """


class SessionExpired(PortalError):
    """An authenticated request was answered as if logged out.

    Raised internally to trigger a single automatic re-login. If it surfaces
    to the caller, the session could not be recovered even on a fresh login —
    a transient failure, never a credentials problem.
    """


class ParseError(PortalError):
    """The portal answered in a shape we do not recognise.

    This is the early-warning signal that the portal has changed — including
    "The Apex request is invalid.", which is what a renamed or redeployed Apex
    class looks like from the outside.
    """
