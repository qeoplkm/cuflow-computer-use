"""Tenant configurations of the same vendor product.

Two institutions run "MockCore Teller" with different branding, version and *labels*.
Internals that tenants cannot configure (routes, form field names) stay identical — which is
the realistic split we exploit for cross-tenant artifact reuse.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tenant:
    key: str
    name: str
    color: str
    version: str
    member_no_label: str
    inquiry_menu: str
    search_button: str
    balance_header: str


TENANTS: dict[str, Tenant] = {
    "harbor": Tenant(
        key="harbor",
        name="Harbor Example Credit Union",
        color="#1f3a5f",
        version="4.2.1",
        member_no_label="Member Number:",
        inquiry_menu="Member Inquiry",
        search_button="Search",
        balance_header="Balance",
    ),
    "summit": Tenant(
        key="summit",
        name="Summit Example Bank",
        color="#6b1f2a",
        version="4.3.0",
        member_no_label="Member #:",
        inquiry_menu="Member Lookup",
        search_button="Find",
        balance_header="Current Balance",
    ),
}
