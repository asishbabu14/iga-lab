"""Errors raised by the lab. None of these represent a live tenant."""


class IgaError(Exception):
    """Base error for iga-lab."""


class LoadError(IgaError):
    """Seed data, policy, or campaign files are invalid."""


class FeedError(IgaError):
    """An HR feed event cannot be applied."""


class CampaignError(IgaError):
    """A certification campaign cannot be applied."""


class ConnectorError(IgaError):
    """A mock connector could not write desired state."""
