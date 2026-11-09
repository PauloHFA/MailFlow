from collections import defaultdict

import dns.resolver
import pytest

from src.domain.checks import check_domain_records


class FakeAnswer:
    def __init__(self, records: list[str]) -> None:
        self.records = records

    def __iter__(self):
        return iter(self.records)


class FakeResolver:
    def __init__(self, records: dict[str, list[str]]) -> None:
        self.records = defaultdict(list, records)

    def resolve(self, name: str, record_type: str, **_kwargs):
        records = self.records.get((name, record_type), [])
        if not records:
            raise dns.resolver.NoAnswer
        return FakeAnswer(records)


def test_domain_check_identifies_spf_dkim_and_dmarc_records() -> None:
    resolver = FakeResolver(
        {
            ("example.com", "TXT"): ['"v=spf1 include:mail.example -all"'],
            ("_dmarc.example.com", "TXT"): ['"v=DMARC1; p=reject; rua=mailto:dmarc@example.com"'],
            ("mail._domainkey.example.com", "TXT"): ['"v=DKIM1; k=rsa; p=publickey"'],
        }
    )

    result = check_domain_records(
        "Example.COM.",
        ["mail"],
        resolver=resolver,
    )

    assert result["domain"] == "example.com"
    assert result["spf"]["status"] == "pass"
    assert result["dmarc"]["status"] == "pass"
    assert result["dmarc"]["policy"] == "reject"
    assert result["dkim"]["selectors"]["mail"]["status"] == "pass"
    assert result["pending"] == []


def test_domain_check_lists_missing_records_and_selectors() -> None:
    result = check_domain_records(
        "example.com",
        ["selector1"],
        resolver=FakeResolver({}),
    )

    assert result["spf"]["status"] == "missing"
    assert result["dmarc"]["status"] == "missing"
    assert result["dkim"]["status"] == "missing"
    assert len(result["pending"]) == 3


@pytest.mark.parametrize(
    "domain,selectors",
    [
        ("bad domain.example", []),
        ("example.com", ["selector.with.dot"]),
        ("example.com", ["x" * 64]),
    ],
)
def test_domain_check_rejects_invalid_domains_or_selectors(
    domain: str,
    selectors: list[str],
) -> None:
    with pytest.raises(ValueError):
        check_domain_records(domain, selectors, resolver=FakeResolver({}))
