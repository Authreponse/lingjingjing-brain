"""Domain exceptions - Core business logic exceptions."""

from __future__ import annotations


class DomainException(Exception):
    """Base exception for domain errors."""
    
    def __init__(self, message: str, code: str = "DOMAIN_ERROR", details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


class ValidationError(DomainException):
    """Raised when domain validation fails."""
    
    def __init__(self, message: str, field: str | None = None, value: any = None):
        super().__init__(
            message,
            code="VALIDATION_ERROR",
            details={"field": field, "value": value} if field else {}
        )
        self.field = field
        self.value = value


class NotFoundError(DomainException):
    """Raised when entity is not found."""
    
    def __init__(self, entity_type: str, identifier: str):
        super().__init__(
            f"{entity_type} not found: {identifier}",
            code="NOT_FOUND",
            details={"entity_type": entity_type, "identifier": identifier}
        )


class ConflictError(DomainException):
    """Raised when there's a conflict (e.g., duplicate)."""
    
    def __init__(self, message: str, resource: str, identifier: str):
        super().__init__(
            message,
            code="CONFLICT",
            details={"resource": resource, "identifier": identifier}
        )


class BusinessRuleViolation(DomainException):
    """Raised when a business rule is violated."""
    
    def __init__(self, rule: str, message: str):
        super().__init__(
            message,
            code="BUSINESS_RULE_VIOLATION",
            details={"rule": rule}
        )


class UnauthorizedError(DomainException):
    """Raised when authorization fails."""
    
    def __init__(self, message: str = "Unauthorized"):
        super().__init__(
            message,
            code="UNAUTHORIZED"
        )


class ForbiddenError(DomainException):
    """Raised when access is forbidden."""
    
    def __init__(self, message: str = "Forbidden", resource: str | None = None):
        super().__init__(
            message,
            code="FORBIDDEN",
            details={"resource": resource} if resource else {}
        )


class RateLimitExceeded(DomainException):
    """Raised when rate limit is exceeded."""
    
    def __init__(self, message: str = "Rate limit exceeded", retry_after: int | None = None):
        super().__init__(
            message,
            code="RATE_LIMIT_EXCEEDED",
            details={"retry_after": retry_after} if retry_after else {}
        )


class ExternalServiceError(DomainException):
    """Raised when external service fails."""
    
    def __init__(self, service: str, message: str, retryable: bool = True):
        super().__init__(
            f"External service {service} error: {message}",
            code="EXTERNAL_SERVICE_ERROR",
            details={"service": service, "retryable": True}
        )


class ConfigurationError(DomainException):
    """Raised when configuration is invalid."""
    
    def __init__(self, message: str, config_key: str | None = None):
        super().__init__(
            message,
            code="CONFIGURATION_ERROR",
            details={"config_key": config_key} if config_key else {}
        )
