"""Explicit public views keep provider credentials outside API responses."""

from datetime import datetime

from sqlalchemy import inspect


def public(row):
    result = {}
    for field in inspect(type(row)).column_attrs:
        if field.key in {
            "credential",
            "fingerprint",
            "lease_until",
            "observer_owner",
            "observer_until",
        }:
            continue
        value = getattr(row, field.key)
        result[field.key] = value.isoformat() if isinstance(value, datetime) else value
    return result
