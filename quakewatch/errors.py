"""Expected pipeline failures. Each one makes the CLI exit non-zero without publishing."""


class QuakeWatchError(Exception):
    """Base class for failures the pipeline reports and exits on."""


class FetchError(QuakeWatchError):
    """The feed could not be fetched safely."""


class DisallowedURLError(FetchError):
    """A URL outside the HTTPS allowlist was requested, directly or via a redirect."""


class ResponseTooLargeError(FetchError):
    """The response body passed the size limit."""


class FeedFormatError(QuakeWatchError):
    """The payload is not valid JSON or not a GeoJSON FeatureCollection."""


class DataQualityError(QuakeWatchError):
    """Too many rows were rejected to trust this feed."""


class UnsafePathError(QuakeWatchError):
    """The requested paths would publish something that must stay private."""
