"""OutcomeCI portable workflow runtime."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("outcomeci-cli")
except PackageNotFoundError:
    __version__ = "0.0.0"
