"""Audit the complete gateway and GPU package locks without downloading models."""

from security import dependency_audit

if __name__ == "__main__":
    try:
        dependency_audit()
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
