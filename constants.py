"""
Direct Python replica of constants.rs.

Rust uses `pub const` for compile-time constants and a `&[FieldConstraint]`
(a static slice of structs) for FIELD_CONSTRAINTS. Python has no const
enforcement or static slices, so these are module-level bindings — by
convention (ALL_CAPS) they are treated as constants and must not be
reassigned anywhere else in the codebase.
"""

from dataclasses import dataclass

ONE_KILO_BYTE: int = 1024

MAX_CONTENT_LENGTH: int = 50 * ONE_KILO_BYTE
MAX_FORM_DATA_LENGTH: int = 10 * ONE_KILO_BYTE

WINDOW_LIMIT_MINS: int = 60  # 1 hour = 60 min
CORS_CONFIG_MAX_AGE: int = 86400
CORS_CONFIG_MIN_AGE: int = CORS_CONFIG_MAX_AGE // 24  # Rust integer division -> //
MAX_REQUEST_LINE_SIZE: int = 8192
MAX_HEADER_LINE_SIZE: int = 8192

RFC_5321_MAX_EMAIL_LENGTH: int = 254
RFC_5321_MAX_LOCAL_PART_LENGTH: int = 64
RFC_5321_MAX_DOMAIN_PART_LENGTH: int = 253


@dataclass(frozen=True)
class FieldConstraint:
    """
    Replica of Rust's FieldConstraint struct.

    `frozen=True` mirrors Rust's immutability of `pub const` data —
    instances cannot be mutated after construction, matching the
    guarantees the Rust struct gets for free as a `&'static` const slice.
    """

    name: str
    max_length: int
    required: bool
    email: bool  # true for email field, enables regex validation


# Rust: pub const FIELD_CONSTRAINTS: &[FieldConstraint] = &[ ... ];
FIELD_CONSTRAINTS: tuple[FieldConstraint, ...] = (
    FieldConstraint(name="name", max_length=40, required=True, email=False),
    FieldConstraint(name="email", max_length=80, required=True, email=True),
    FieldConstraint(name="message", max_length=2000, required=True, email=False),
    FieldConstraint(name="frontend", max_length=10, required=False, email=False),
    FieldConstraint(name="webDevelopment", max_length=10, required=False, email=False),
    FieldConstraint(name="blender", max_length=10, required=False, email=False),
)

# Rust: pub const OPTIONAL_CHECKBOX: [&str; 3] = [...];
OPTIONAL_CHECKBOX: tuple[str, str, str] = ("blender", "frontend", "webDevelopment")
