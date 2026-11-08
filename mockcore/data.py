"""Synthetic member data. Every value here is fabricated; no real PII."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    suffix: str
    type: str
    description: str
    balance: str
    available: str


@dataclass(frozen=True)
class Member:
    number: str
    name: str
    tax_id_last4: str
    address: str
    phone: str
    since: str
    restricted: bool = False
    accounts: tuple[Account, ...] = field(default_factory=tuple)


MEMBERS: dict[str, Member] = {
    m.number: m
    for m in [
        Member(
            number="10042",
            name="SAMPLE, JORDAN Q",
            tax_id_last4="4821",
            address="100 EXAMPLE ST, SPRINGFIELD",
            phone="(555) 010-4242",
            since="03/14/2011",
            accounts=(
                Account("0000", "SHR", "Share Savings", "2,345.67", "2,320.67"),
                Account("0010", "DFT", "Checking", "1,210.09", "1,210.09"),
                Account("0070", "LN", "Auto Loan", "-8,450.00", "0.00"),
            ),
        ),
        # Different account order on purpose: a positional locator would read the wrong row.
        Member(
            number="10051",
            name="EXAMPLE, RIVER T",
            tax_id_last4="1177",
            address="42 PLACEHOLDER AVE, RIVERTON",
            phone="(555) 010-7781",
            since="07/02/2019",
            accounts=(
                Account("0004", "SHR", "Holiday Club", "310.00", "310.00"),
                Account("0010", "DFT", "Checking", "88.12", "88.12"),
                Account("0000", "SHR", "Share Savings", "15,002.11", "15,002.11"),
            ),
        ),
        Member(
            number="10077",
            name="DOE, TAYLOR (EMPLOYEE)",
            tax_id_last4="0901",
            address="1 STAFF WAY, SPRINGFIELD",
            phone="(555) 010-0001",
            since="01/05/2015",
            restricted=True,
            accounts=(Account("0000", "SHR", "Share Savings", "9,999.99", "9,999.99"),),
        ),
        Member(
            number="20013",
            name="TESTER, MORGAN",
            tax_id_last4="5550",
            address="9 MOCK RD, LAKEVIEW",
            phone="(555) 010-3131",
            since="11/21/2023",
            accounts=(Account("0000", "SHR", "Share Savings", "5.00", "5.00"),),
        ),
    ]
}
