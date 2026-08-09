"""Shared failures raised by Palomar's verification boundaries."""


class VerificationError(RuntimeError):
    """Untrusted input or execution violated a verifier contract."""
