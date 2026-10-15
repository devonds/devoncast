"""Exceptions shared across the pipeline."""


class PermanentError(Exception):
    """A failure that retrying won't fix, such as an unsupported file type."""
