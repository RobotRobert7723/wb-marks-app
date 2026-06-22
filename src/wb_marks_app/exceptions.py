class AppError(Exception):
    """Base application error."""


class ConfigurationError(AppError):
    """Raised when application configuration is invalid."""


class MappingValidationError(AppError):
    """Raised when barcode to GTIN mapping contains errors."""


class IntegrationUnavailableError(AppError):
    """Raised when an external integration cannot proceed automatically."""


class ManualStepRequired(AppError):
    """Raised when a manual browser step is required."""

