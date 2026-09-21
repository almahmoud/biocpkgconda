"""Build Bioconductor packages as a conda channel hosted on GitHub Container Registry."""


class Error(Exception):
    """A failure reported to the user without a traceback."""
