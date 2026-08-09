#!/usr/bin/env python3
"""Email checker with automatic fallback-domain rotation.

Checks an email address and, when the target rejects it as "invalid
email" (or any similar rejection), automatically retries the same local
part against the configured fallback domains (e.g. afhamxmailz.com,
mail.draxon.one) until one passes.

Checks performed, in order:
  1. Syntax   - RFC 5321-ish address structure (local-only, no network).
  2. Domain   - DNS MX / host lookup for the domain part (skippable).
  3. Signup   - optional live POST to a signup URL; a response that says
                the email is invalid triggers rotation to the next
                fallback domain and a retry.

Usage:
    python email_checker.py you@example.com
    python email_checker.py you@example.com --url https://site.com/signup
    python email_checker.py --batch emails.txt --url https://site.com/signup --json
    python email_checker.py you@example.com --url https://site.com/signup --quiet

Options:
    --url URL             POST the email to URL and watch for rejections.
    --json                Send the payload as JSON instead of form-encoded.
    --field NAME          Payload field that carries the email (default: email).
    --extra K=V           Extra payload field, repeatable (e.g. --extra name=John).
    --fallback-domains D  Comma-separated domains to rotate to on rejection
                          (default: afhamxmailz.com,mail.draxon.one).
    --no-mx               Skip DNS/MX checks entirely.
    --syntax-only         Only validate syntax; make no network calls.
    --batch FILE          Read one email per line from FILE and check each.
    --timeout SECONDS     HTTP timeout (default: 15).
    --quiet               Print only the final working email (per line in batch).
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from dataclasses import dataclass, field
from typing import Iterable, Optional

try:  # requests is listed in requirements.txt; degrade to a clear error if missing
    import requests  # type: ignore
except ImportError:  # pragma: no cover
    requests = None

DEFAULT_FALLBACK_DOMAINS = ("afhamxmailz.com", "mail.draxon.one")

# Substrings that typically mean "this email is not acceptable here".
DEFAULT_REJECTION_PATTERNS = (
    "invalid email",
    "invalid e-mail",
    "email address is not valid",
    "email is not valid",
    "enter a valid email",
    "enter valid email",
    "not a valid email",
    "email not recognized",
    "email address is invalid",
    "email address not valid",
    "invalid email address",
    "unknown email",
    "email not found",
    "no account found for this email",
    "email format",
)

_LOCAL_PART_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)


def validate_email_syntax(email: str) -> tuple[bool, str]:
    """Basic syntactic validation of an email address.

    Returns (ok, reason). Never touches the network.
    """
    email = (email or "").strip()
    if not email:
        return False, "empty address"
    if email.count("@") != 1:
        return False, "must contain exactly one '@'"
    local, _, domain = email.partition("@")
    if not 1 <= len(local) <= 64 or not _LOCAL_PART_RE.match(local):
        return False, f"invalid local part: {local!r}"
    if not 1 <= len(domain) <= 253 or not _DOMAIN_RE.match(domain):
        return False, f"invalid domain part: {domain!r}"
    return True, "ok"


def check_domain(domain: str) -> tuple[Optional[bool], list[str]]:
    """Check whether the domain can receive mail.

    Returns (domain_ok, mx_hosts) where domain_ok is True, False, or None
    (None = could not confirm either way; e.g. dnspython not installed but
    the domain resolves).
    """
    try:
        import dns.resolver  # dnspython, optional

        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=5)
            mx = [str(r.exchange).rstrip(".") for r in answers]
            return True, mx
        except dns.resolver.NXDOMAIN:
            return False, []
        except dns.resolver.NoAnswer:
            # No MX records: fall through to a plain resolution check.
            pass
        except Exception:
            pass
    except ImportError:
        pass

    try:
        socket.getaddrinfo(domain, None)
    except socket.gaierror:
        return False, []
    return None, []


def _looks_like_rejection(text: str, patterns: Iterable[str]) -> Optional[str]:
    low = text.lower()
    for pattern in patterns:
        if pattern in low:
            return pattern
    return None


def check_signup(
    email: str,
    url: str,
    field: str = "email",
    as_json: bool = False,
    timeout: float = 15.0,
    patterns: Iterable[str] = DEFAULT_REJECTION_PATTERNS,
    extra_fields: Optional[dict] = None,
) -> tuple[bool, str, Optional[int]]:
    """POST an email to a signup URL and look for 'invalid email' rejections.

    Returns (rejected, reason, http_status). `rejected` is True only when
    the response text matches a rejection pattern (other failures return
    rejected=False with their reason, so rotation is not triggered).
    """
    if requests is None:
        return True, "requests library not installed", None

    payload = dict(extra_fields or {})
    payload[field] = email

    try:
        if as_json:
            resp = requests.post(
                url,
                json=payload,
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            )
        else:
            resp = requests.post(url, data=payload, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"network error: {exc}", None

    try:
        body = json.dumps(resp.json())
    except ValueError:
        body = resp.text or ""

    hit = _looks_like_rejection(body + " " + (resp.reason or ""), patterns)
    if hit:
        return True, f"rejected ({hit!r})", resp.status_code

    status = resp.status_code
    if 200 <= status < 300:
        return False, f"http {status}: accepted", status
    return False, f"http {status}: {resp.reason or 'non-email error'}", status


def candidate_emails(
    email: str, fallback_domains: Iterable[str] = DEFAULT_FALLBACK_DOMAINS
) -> list[str]:
    """The original email plus the same local part on each fallback domain."""
    email = (email or "").strip()
    local = email.split("@", 1)[0] if "@" in email else email
    seen: set[str] = set()
    out: list[str] = []
    for addr in (email, *(f"{local}@{d}" for d in fallback_domains)):
        if addr and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


@dataclass
class CheckResult:
    """Outcome of a full check (all rotation attempts)."""

    requested: str
    ok: bool = False
    reason: str = ""
    attempts: list[tuple[str, str, str]] = field(default_factory=list)  # (email, status, note)


def find_working_email(
    email: str,
    url: Optional[str] = None,
    field: str = "email",
    as_json: bool = False,
    check_mx: bool = True,
    timeout: float = 15.0,
    fallback_domains: Iterable[str] = DEFAULT_FALLBACK_DOMAINS,
    patterns: Iterable[str] = DEFAULT_REJECTION_PATTERNS,
    extra_fields: Optional[dict] = None,
) -> tuple[Optional[str], CheckResult]:
    """Try the email, then each fallback domain, until one passes.

    A candidate is rejected (and rotation continues) when its syntax is
    invalid, its domain is confirmed dead, or the signup endpoint answers
    with an 'invalid email' style rejection. Returns (winner, result).
    """
    result = CheckResult(requested=email)
    domain_ok: Optional[bool] = True  # None = unknown, do not skip on it

    for candidate in candidate_emails(email, fallback_domains):
        ok, err = validate_email_syntax(candidate)
        if not ok:
            result.attempts.append((candidate, "invalid", f"syntax: {err}"))
            continue

        if check_mx:
            domain_ok, mx = check_domain(candidate.split("@", 1)[1])
            if domain_ok is False:
                result.attempts.append(
                    (candidate, "invalid", "domain has no MX records / does not resolve")
                )
                continue

        if url:
            rejected, reason, status = check_signup(
                candidate,
                url,
                field=field,
                as_json=as_json,
                timeout=timeout,
                patterns=patterns,
                extra_fields=extra_fields,
            )
            result.attempts.append((candidate, "rejected" if rejected else "ok", reason))
            if not rejected:
                result.ok = True
                result.reason = reason
                return candidate, result
            continue

        result.ok = True
        result.reason = f"valid (mx: {domain_ok})"
        result.attempts.append((candidate, "ok", "syntax + domain checks passed"))
        return candidate, result

    result.reason = "no working email found"
    return None, result


def _parse_extra(fields: list[str]) -> dict:
    out: dict = {}
    for item in fields:
        if "=" not in item:
            raise argparse.ArgumentTypeError(f"--extra expects K=V, got {item!r}")
        key, _, value = item.partition("=")
        out[key] = value
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="email_checker",
        description="Validate emails and rotate to fallback domains on 'invalid email' rejections.",
        epilog=(
            "Fallback domains default to afhamxmailz.com and mail.draxon.one; "
            "override with --fallback-domains or the EMAIL_FALLBACK_DOMAINS env var."
        ),
    )
    parser.add_argument("email", nargs="?", help="email address to check")
    parser.add_argument("--url", help="signup URL to POST the email to")
    parser.add_argument("--json", action="store_true", help="send JSON payload instead of form data")
    parser.add_argument("--field", default="email", help="payload field carrying the email (default: email)")
    parser.add_argument("--extra", action="append", default=[], metavar="K=V", help="extra payload field, repeatable")
    parser.add_argument(
        "--fallback-domains",
        default=None,
        help="comma-separated fallback domains (default: afhamxmailz.com,mail.draxon.one)",
    )
    parser.add_argument("--no-mx", action="store_true", help="skip DNS/MX checks")
    parser.add_argument("--syntax-only", action="store_true", help="only validate syntax, no network")
    parser.add_argument("--batch", help="file with one email per line to check")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds (default: 15)")
    parser.add_argument("--quiet", action="store_true", help="print only the winning email")
    args = parser.parse_args(argv)

    fallback_domains = tuple(
        d.strip().lower()
        for d in (
            args.fallback_domains
            or __import__("os").environ.get("EMAIL_FALLBACK_DOMAINS", ",".join(DEFAULT_FALLBACK_DOMAINS))
        ).split(",")
        if d.strip()
    )

    emails: list[str] = []
    if args.batch:
        try:
            with open(args.batch, encoding="utf-8") as fh:
                emails = [line.strip() for line in fh if line.strip()]
        except OSError as exc:
            print(f"error: cannot read {args.batch}: {exc}", file=sys.stderr)
            return 1
    elif args.email:
        emails = [args.email]
    else:
        parser.error("an email address or --batch FILE is required")

    if args.syntax_only:
        all_ok = True
        for email in emails:
            ok, err = validate_email_syntax(email)
            if args.quiet:
                print(email if ok else "", end="\n")
            else:
                print(f"{email}: {'OK' if ok else 'INVALID'} ({err})")
            all_ok = all_ok and ok
        return 0 if all_ok else 1

    extra = _parse_extra(args.extra)
    exit_code = 0
    for email in emails:
        winner, res = find_working_email(
            email,
            url=args.url,
            field=args.field,
            as_json=args.json,
            check_mx=not args.no_mx,
            timeout=args.timeout,
            fallback_domains=fallback_domains,
            extra_fields=extra or None,
        )
        if args.quiet:
            print(winner if winner else "", end="\n")
        else:
            print(f"== {email}")
            for addr, status, note in res.attempts:
                print(f"   [{status:>9}] {addr}  {note}")
            print(f"   {'PASS' if winner else 'FAIL'}: {res.reason}")
        if winner is None:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
