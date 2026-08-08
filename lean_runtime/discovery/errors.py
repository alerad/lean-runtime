"""Errors raised by Lean Runtime environment discovery."""


class DiscoveryError(Exception):
    """Base class for discovery failures."""


class CatalogError(DiscoveryError):
    """The discovery catalog is malformed or cannot be loaded."""


class PolicyError(DiscoveryError):
    """A discovery policy is invalid."""
