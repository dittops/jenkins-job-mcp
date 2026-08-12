"""Typed errors surfaced to MCP clients.

Every message here ends up in a tool result, so nothing in this module may
carry the authtoken. Use ``itsm_mcp.client.redact`` before embedding a URL.
"""


class ItsmMCPError(Exception):
    """Base class for every error this server raises deliberately."""


class ConfigError(ItsmMCPError):
    """Environment or policy file is missing/invalid."""


class UnknownGroupError(ItsmMCPError):
    """Requested group is not in the policy's allowlist."""


class ParameterError(ItsmMCPError):
    """Supplied arguments do not describe a valid update."""


class ItsmAPIError(ItsmMCPError):
    """ITSM returned an unexpected status or a failure envelope."""


class ItsmAuthError(ItsmMCPError):
    """ITSM rejected the authtoken (or the token lacks the permission)."""


class TicketNotFound(ItsmMCPError):
    """No request exists with the given id."""


class ItsmConnectionError(ItsmMCPError):
    """ITSM could not be reached at all (DNS, refused, timeout, TLS)."""
