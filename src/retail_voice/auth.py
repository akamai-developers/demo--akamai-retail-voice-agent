import os
import secrets

from fastapi import HTTPException


def valid_key(value):
    return (
        isinstance(value, str)
        and value.isascii()
        and 16 <= len(value) <= 256
        and value.isprintable()
        and not any(c.isspace() for c in value)
    )


class Credentials:
    def __init__(self, tenants=None, operator=None):
        if tenants is None:
            a, b = os.getenv("TENANT_A_KEY"), os.getenv("TENANT_B_KEY")
            tenants = {a: "retailer-a", b: "retailer-b"}
        if operator is None:
            operator = os.getenv("OPERATOR_KEY")
        if (
            len(tenants) != 2
            or set(tenants.values()) != {"retailer-a", "retailer-b"}
            or not all(valid_key(k) for k in tenants)
            or not valid_key(operator)
            or operator in tenants
        ):
            raise RuntimeError(
                "Configure two distinct tenant keys and a distinct operator key: at least 16 ASCII characters, no whitespace"
            )
        self.tenants, self.operator = dict(tenants), operator

    @staticmethod
    def bearer(header):
        if not header or not header.startswith("Bearer ") or not valid_key(header[7:]):
            raise HTTPException(401, "Invalid bearer credential")
        return header[7:]

    def tenant(self, header):
        key = self.bearer(header)
        for candidate, tenant in self.tenants.items():
            if secrets.compare_digest(candidate.encode(), key.encode()):
                return tenant
        raise HTTPException(401, "Invalid credential")

    def require_operator(self, header):
        if not secrets.compare_digest(self.bearer(header).encode(), self.operator.encode()):
            raise HTTPException(403, "Operator required")
