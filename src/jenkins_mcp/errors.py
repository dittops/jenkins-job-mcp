"""Typed errors surfaced to MCP clients.

Every message here ends up in a tool result, so nothing in this module may
carry credentials. Use ``jenkins_mcp.client.redact`` before embedding a URL.
"""


class JenkinsMCPError(Exception):
    """Base class for every error this server raises deliberately."""


class ConfigError(JenkinsMCPError):
    """Environment or job registry is missing/invalid."""


class UnknownActionError(JenkinsMCPError):
    """Requested action is not in the registry."""


class ParameterError(JenkinsMCPError):
    """Supplied build parameters do not match the registry entry."""


class JenkinsAPIError(JenkinsMCPError):
    """Jenkins returned an unexpected status or payload."""


class JenkinsConnectionError(JenkinsMCPError):
    """Jenkins could not be reached at all (DNS, refused, timeout, TLS)."""


class JenkinsTimeout(JenkinsMCPError):
    """Queue wait or build wait exceeded the allowed deadline."""


class BuildCancelled(JenkinsMCPError):
    """The queue item was cancelled before it produced a build."""
