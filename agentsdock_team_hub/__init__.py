"""Standalone identity and collaboration service for AgentsDock Team Hub."""

from .auth import (
    AuthenticationError,
    AuthorizationError,
    BootstrapResult,
    EnrollmentResult,
    IssuedSecret,
    bootstrap_personal_team,
    issue_invitation,
    issue_node_enrollment,
    record_legacy_server_binding,
    redeem_invitation,
    redeem_node_enrollment,
)
from .database import LATEST_SCHEMA_VERSION, apply_migrations, open_database

__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "BootstrapResult",
    "EnrollmentResult",
    "IssuedSecret",
    "LATEST_SCHEMA_VERSION",
    "apply_migrations",
    "bootstrap_personal_team",
    "issue_invitation",
    "issue_node_enrollment",
    "open_database",
    "record_legacy_server_binding",
    "redeem_invitation",
    "redeem_node_enrollment",
]
