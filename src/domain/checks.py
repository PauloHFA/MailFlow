import re
from collections.abc import Iterable
from typing import Protocol, TypedDict

import dns.exception
import dns.resolver

_SELECTOR_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_TAG_PATTERN = re.compile(r"(?:^|;)\s*([A-Za-z][A-Za-z0-9_-]*)\s*=\s*([^;]*)")


class TxtResolver(Protocol):
    timeout: float
    lifetime: float

    def resolve(
        self,
        qname: str,
        rdtype: str,
        **kwargs: object,
    ) -> Iterable[object]: ...


class RecordCheck(TypedDict):
    status: str
    details: str


def _normalize_domain(domain: str) -> str:
    if (
        not domain
        or domain != domain.strip()
        or domain.endswith("..")
        or any(char.isspace() for char in domain)
    ):
        raise ValueError("Domain must be a valid DNS name.")
    try:
        normalized = domain.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise ValueError("Domain must be a valid IDNA DNS name.") from error
    labels = normalized.split(".")
    if (
        not normalized
        or len(normalized) > 253
        or len(labels) < 2
        or any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in labels
        )
    ):
        raise ValueError("Domain must be a valid DNS name.")
    return normalized


def _normalize_txt_record(record: object) -> str:
    strings = getattr(record, "strings", None)
    if strings is not None:
        return b"".join(strings).decode("utf-8", errors="replace")
    return str(record).strip('"')


def _query_txt(name: str, resolver: TxtResolver) -> list[str]:
    try:
        answers = resolver.resolve(name, "TXT", lifetime=4.0)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    return [_normalize_txt_record(answer) for answer in answers]


def _lookup_status(name: str, resolver: TxtResolver) -> tuple[str, list[str]]:
    try:
        return "ok", _query_txt(name, resolver)
    except (dns.exception.Timeout, dns.resolver.NoNameservers):
        return "error", []


def _tags(record: str) -> dict[str, str]:
    return {
        name.casefold(): value.strip()
        for name, value in _TAG_PATTERN.findall(record)
    }


def _check_spf(domain: str, resolver: TxtResolver) -> dict[str, str]:
    lookup_status, records = _lookup_status(domain, resolver)
    if lookup_status == "error":
        return {"status": "error", "details": "DNS lookup failed."}
    spf_records = [
        record
        for record in records
        if re.match(r"^v=spf1(?:\s|$)", record, flags=re.IGNORECASE)
    ]
    if not spf_records:
        return {"status": "missing", "details": "No SPF TXT record was found."}
    if len(spf_records) > 1:
        return {"status": "invalid", "details": "Multiple SPF records were found."}
    return {"status": "pass", "details": "One SPF record was found."}


def _check_dmarc(domain: str, resolver: TxtResolver) -> dict[str, str | None]:
    lookup_status, records = _lookup_status(f"_dmarc.{domain}", resolver)
    if lookup_status == "error":
        return {"status": "error", "policy": None, "details": "DNS lookup failed."}
    dmarc_records = [
        record
        for record in records
        if re.match(r"^v=dmarc1(?:\s|;|$)", record, flags=re.IGNORECASE)
    ]
    if not dmarc_records:
        return {"status": "missing", "policy": None, "details": "No DMARC record was found."}
    if len(dmarc_records) > 1:
        return {"status": "invalid", "policy": None, "details": "Multiple DMARC records were found."}
    policy = _tags(dmarc_records[0]).get("p", "").casefold()
    if policy not in {"none", "quarantine", "reject"}:
        return {"status": "invalid", "policy": None, "details": "DMARC policy is missing or invalid."}
    return {"status": "pass", "policy": policy, "details": "A valid DMARC policy was found."}


def _check_dkim(
    domain: str,
    selectors: list[str],
    resolver: TxtResolver,
) -> dict[str, object]:
    if not selectors:
        return {
            "status": "not_checked",
            "selectors": {},
            "details": "Provide DKIM selectors to check published keys.",
        }
    selector_results: dict[str, RecordCheck] = {}
    for selector in selectors:
        lookup_status, records = _lookup_status(
            f"{selector}._domainkey.{domain}",
            resolver,
        )
        if lookup_status == "error":
            selector_results[selector] = {
                "status": "error",
                "details": "DNS lookup failed.",
            }
            continue
        if not records:
            selector_results[selector] = {
                "status": "missing",
                "details": "No DKIM TXT record was found for this selector.",
            }
            continue
        keys = [record for record in records if _tags(record).get("p") is not None]
        if len(keys) != 1 or not _tags(keys[0]).get("p"):
            selector_results[selector] = {
                "status": "invalid",
                "details": "DKIM record is ambiguous or has an empty public key.",
            }
        else:
            selector_results[selector] = {
                "status": "pass",
                "details": "A DKIM public key was found.",
            }
    statuses = {record["status"] for record in selector_results.values()}
    status = (
        "pass"
        if statuses == {"pass"}
        else "error"
        if "error" in statuses
        else "invalid"
        if "invalid" in statuses
        else "missing"
    )
    return {
        "status": status,
        "selectors": selector_results,
        "details": "DKIM selectors checked.",
    }


def check_domain_records(
    domain: str,
    dkim_selectors: list[str] | None = None,
    *,
    resolver: TxtResolver | None = None,
) -> dict[str, object]:
    normalized_domain = _normalize_domain(domain)
    selectors = [selector.casefold() for selector in (dkim_selectors or [])]
    if len(selectors) > 10 or len(set(selectors)) != len(selectors):
        raise ValueError("Provide at most 10 unique DKIM selectors.")
    if any(not _SELECTOR_PATTERN.fullmatch(selector) for selector in selectors):
        raise ValueError("DKIM selectors must be valid single DNS labels.")

    active_resolver = resolver or dns.resolver.Resolver()
    active_resolver.timeout = 2.0
    active_resolver.lifetime = 4.0
    spf = _check_spf(normalized_domain, active_resolver)
    dmarc = _check_dmarc(normalized_domain, active_resolver)
    dkim = _check_dkim(normalized_domain, selectors, active_resolver)

    pending: list[str] = []
    if spf["status"] != "pass":
        pending.append("Publish one valid SPF TXT record.")
    if dmarc["status"] != "pass":
        pending.append("Publish one valid DMARC TXT record with a policy.")
    if dkim["status"] == "not_checked":
        pending.append("Provide one or more DKIM selectors to verify DKIM.")
    elif dkim["status"] != "pass":
        pending.append("Publish valid DKIM keys for all configured selectors.")

    return {
        "domain": normalized_domain,
        "spf": spf,
        "dkim": dkim,
        "dmarc": dmarc,
        "pending": pending,
    }
