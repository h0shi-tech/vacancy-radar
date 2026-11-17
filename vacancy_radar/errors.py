"""Exception hierarchy for expected vacancy-radar failures.

Everything in this module is an expected, user-facing failure: the CLI catches
:class:`RadarError` and prints a short message to stderr instead of a traceback.
Programming errors are deliberately *not* converted into these exceptions.
"""

from __future__ import annotations


class RadarError(Exception):
    """Base class for expected application errors."""


class DataError(RadarError):
    """A payload (API response or fixture) could not be interpreted."""


class HHNetworkError(RadarError):
    """The HTTP request itself failed (DNS, timeout, connection reset, ...)."""


class HHApiError(RadarError):
    """hh.ru answered, but the response was unusable."""


class FixtureError(RadarError):
    """An offline fixture could not be read or parsed."""
