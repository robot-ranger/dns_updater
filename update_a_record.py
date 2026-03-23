#!/usr/bin/env python3
"""Update an A record in cPanel Zone Editor via UAPI.

Uses cPanel UAPI endpoints:
- /execute/ZoneEdit/fetchzone_records
- /execute/ZoneEdit/edit_zone_record

Example:
  python3 update_a_record.py \
    --host cp.example.com \
    --user cpaneluser \
    --token YOUR_API_TOKEN \
    --domain example.com \
        --name app.example.com \
    --ttl 300
"""

from __future__ import annotations
import ipaddress

import argparse
import logging
import os
import sys
import dotenv
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

dotenv.load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

class CpanelApiError(RuntimeError):
    """Raised when cPanel UAPI returns an error payload."""


def setup_logging(verbose: bool = False) -> logging.Logger:
    log_path = Path(__file__).with_name("update_a_record.log")
    logger = logging.getLogger("update_a_record")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(file_handler)

    if verbose and not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stdout for h in logger.handlers):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter("[verbose] %(message)s"))
        logger.addHandler(console_handler)

    return logger


def log_success(logger: logging.Logger, before: Dict[str, Any], after: Dict[str, Any]) -> None:
    logger.info(
        "Success: Old: %s New: %s",
        {
            "line": before.get("line"),
            "name": before.get("name"),
            "type": before.get("type"),
            "address": before.get("address"),
            "ttl": before.get("ttl"),
        },
        {
            "line": after.get("line"),
            "name": after.get("name"),
            "type": after.get("type"),
            "address": after.get("address"),
            "ttl": after.get("ttl"),
        },
    )


def log_error(logger: logging.Logger, message: str) -> None:
    logger.error("Error: %s", message)


def _error_mentions_missing_uapi_zoneedit(message: str) -> bool:
    text = (message or "").lower()
    return (
        "failed to load module" in text
        and "zoneedit" in text
        and "cpanel::api::zoneedit" in text
    )


def build_session(user: str, token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"cpanel {user}:{token}",
            "Accept": "application/json",
        }
    )
    return session


def detect_public_ipv4(timeout: int) -> str:
    services = (
        "https://api.ipify.org",
        "https://checkip.amazonaws.com",
        "https://ifconfig.me/ip",
    )

    last_error: Optional[Exception] = None
    logger = logging.getLogger("update_a_record")

    for url in services:
        logger.debug("Trying public IP detection endpoint: %s", url)
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            candidate = response.text.strip()
            ipaddress.IPv4Address(candidate)
            logger.debug("Public IPv4 detected via %s: %s", url, candidate)
            return candidate
        except (requests.RequestException, ValueError) as exc:
            logger.debug("IP detection failed via %s: %s", url, exc)
            last_error = exc

    raise CpanelApiError(f"Unable to detect public IPv4 address: {last_error}")


def call_uapi(
    session: requests.Session,
    host: str,
    module: str,
    function: str,
    params: Dict[str, Any],
    verify_ssl: bool,
    timeout: int,
) -> Dict[str, Any]:
    logger = logging.getLogger("update_a_record")
    url = f"https://{host}:2083/execute/{module}/{function}"
    logger.debug("Calling UAPI %s/%s with params: %s", module, function, params)
    resp = session.get(url, params=params, timeout=timeout, verify=verify_ssl)
    resp.raise_for_status()

    try:
        payload = resp.json()
    except ValueError as exc:
        raise CpanelApiError(f"Non-JSON response from cPanel: {resp.text[:500]}") from exc

    status = payload.get("status")
    errors = payload.get("errors") or []
    if status != 1 or errors:
        msg = "; ".join(errors) if errors else f"Unknown API error payload: {payload}"
        raise CpanelApiError(msg)

    return payload


def call_api2_zoneedit(
    session: requests.Session,
    host: str,
    user: str,
    function: str,
    params: Dict[str, Any],
    verify_ssl: bool,
    timeout: int,
) -> Dict[str, Any]:
    logger = logging.getLogger("update_a_record")
    url = f"https://{host}:2083/json-api/cpanel"
    query = {
        "cpanel_jsonapi_apiversion": 2,
        "cpanel_jsonapi_user": user,
        "cpanel_jsonapi_module": "ZoneEdit",
        "cpanel_jsonapi_func": function,
    }
    query.update(params)
    logger.debug("Calling API2 ZoneEdit/%s with params: %s", function, params)

    resp = session.get(url, params=query, timeout=timeout, verify=verify_ssl)
    resp.raise_for_status()

    try:
        payload = resp.json()
    except ValueError as exc:
        raise CpanelApiError(f"Non-JSON response from cPanel API2: {resp.text[:500]}") from exc

    cpanelresult = payload.get("cpanelresult") or {}
    if cpanelresult.get("error"):
        raise CpanelApiError(str(cpanelresult["error"]))

    event = cpanelresult.get("event") or {}
    if event.get("result") == 0:
        reason = event.get("reason") or "Unknown API2 error"
        raise CpanelApiError(str(reason))

    data = cpanelresult.get("data")
    if not isinstance(data, list):
        raise CpanelApiError(f"Unexpected API2 response format: {payload}")

    return payload


def normalize_name(name: str) -> str:
    # Zone records usually include a trailing dot in cPanel output.
    return name.rstrip(".").lower()


def pick_record(records: List[Dict[str, Any]], name: str, line: Optional[int]) -> Dict[str, Any]:
    logger = logging.getLogger("update_a_record")
    if line is not None:
        logger.debug("Selecting record by explicit line: %s", line)
        for rec in records:
            if int(rec.get("line", -1)) == line:
                return rec
        raise CpanelApiError(f"No record found with line={line}.")

    wanted = normalize_name(name)
    candidates = []
    for rec in records:
        rec_type = str(rec.get("type", "")).upper()
        rec_name = normalize_name(str(rec.get("name", "")))
        if rec_type == "A" and rec_name == wanted:
            candidates.append(rec)

    if not candidates:
        raise CpanelApiError(f"No A record found for name '{name}'.")

    if len(candidates) > 1:
        lines = ", ".join(str(c.get("line")) for c in candidates)
        raise CpanelApiError(
            "Multiple A records matched that name. Re-run with --line. "
            f"Matching lines: {lines}"
        )

    logger.debug("Selected A record line=%s name=%s", candidates[0].get("line"), candidates[0].get("name"))
    return candidates[0]


def fetch_a_records(
    session: requests.Session,
    host: str,
    user: str,
    domain: str,
    verify_ssl: bool,
    timeout: int,
) -> List[Dict[str, Any]]:
    logger = logging.getLogger("update_a_record")
    try:
        payload = call_uapi(
            session=session,
            host=host,
            module="ZoneEdit",
            function="fetchzone_records",
            params={"domain": domain},
            verify_ssl=verify_ssl,
            timeout=timeout,
        )

        data = payload.get("data") or []
        if not isinstance(data, list):
            raise CpanelApiError(f"Unexpected fetch response format: {payload}")
        logger.debug("Fetched %d A records via UAPI", len([r for r in data if str(r.get("type", "")).upper() == "A"]))
        return [r for r in data if str(r.get("type", "")).upper() == "A"]
    except CpanelApiError as exc:
        if not _error_mentions_missing_uapi_zoneedit(str(exc)):
            raise

        logger.debug("UAPI ZoneEdit unavailable, falling back to API2")

        payload = call_api2_zoneedit(
            session=session,
            host=host,
            user=user,
            function="fetchzone_records",
            params={"domain": domain},
            verify_ssl=verify_ssl,
            timeout=timeout,
        )
        data = payload.get("cpanelresult", {}).get("data") or []
        logger.debug("Fetched %d A records via API2 fallback", len([r for r in data if str(r.get("type", "")).upper() == "A"]))
        return [r for r in data if str(r.get("type", "")).upper() == "A"]


def update_a_record(
    session: requests.Session,
    host: str,
    user: str,
    domain: str,
    record: Dict[str, Any],
    new_ip: str,
    ttl: Optional[int],
    verify_ssl: bool,
    timeout: int,
) -> Dict[str, Any]:
    logger = logging.getLogger("update_a_record")
    params: Dict[str, Any] = {
        "domain": domain,
        "line": record["line"],
        "address": new_ip,
    }

    # Preserve existing fields where available so edit is explicit and predictable.
    for key in ("name", "class", "type"):
        if key in record and record[key] is not None:
            params[key] = record[key]

    params["ttl"] = ttl if ttl is not None else record.get("ttl", 14400)
    logger.debug("Updating record line=%s to address=%s ttl=%s", record.get("line"), new_ip, params["ttl"])

    try:
        return call_uapi(
            session=session,
            host=host,
            module="ZoneEdit",
            function="edit_zone_record",
            params=params,
            verify_ssl=verify_ssl,
            timeout=timeout,
        )
    except CpanelApiError as exc:
        if not _error_mentions_missing_uapi_zoneedit(str(exc)):
            raise
        logger.debug("UAPI edit unavailable, falling back to API2")
        return call_api2_zoneedit(
            session=session,
            host=host,
            user=user,
            function="edit_zone_record",
            params=params,
            verify_ssl=verify_ssl,
            timeout=timeout,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update a cPanel A record using UAPI.")
    parser.add_argument("--host", help="cPanel host, e.g. cp.example.com", default=os.getenv("CPANEL_HOST"))
    parser.add_argument(
        "--user",
        help="cPanel username that owns the zone",
        default=os.getenv("CPANEL_USER"),
    )
    parser.add_argument(
        "--token",
        help="cPanel API token",
        default=os.getenv("CPANEL_TOKEN"),
    )
    parser.add_argument("--domain", help="Zone domain, e.g. example.com", default=os.getenv("CPANEL_DOMAIN"))
    parser.add_argument(
        "--name",
        help="Record name, e.g. app.example.com",
        default=os.getenv("CPANEL_RECORD_NAME") or os.getenv("CPANEL_NAME"),
    )
    parser.add_argument("--line", type=int, help="Record line number (recommended if duplicates)")
    parser.add_argument("--ttl", type=int, help="TTL seconds (optional)")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (not recommended)",
    )
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print verbose debug logs to terminal (not written to log file)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed but do not call edit_zone_record",
    )
    args = parser.parse_args()

    missing = [
        flag
        for flag in ("host", "user", "token", "domain", "name")
        if not getattr(args, flag)
    ]
    if missing:
        parser.error(
            "missing required settings (provide via CLI or .env): "
            + ", ".join(f"--{flag}" for flag in missing)
        )

    return args


def main() -> int:
    args = parse_args()
    logger = setup_logging(verbose=args.verbose)
    verify_ssl = not args.insecure
    logger.debug("Starting update with host=%s domain=%s name=%s line=%s dry_run=%s", args.host, args.domain, args.name, args.line, args.dry_run)
    detected_ip = detect_public_ipv4(args.timeout)

    session = build_session(user=args.user, token=args.token)

    try:
        a_records = fetch_a_records(
            session=session,
            host=args.host,
            user=args.user,
            domain=args.domain,
            verify_ssl=verify_ssl,
            timeout=args.timeout,
        )

        target = pick_record(a_records, name=args.name, line=args.line)

        before = {
            "line": target.get("line"),
            "name": target.get("name"),
            "type": target.get("type"),
            "address": target.get("address"),
            "ttl": target.get("ttl"),
        }

        after = dict(before)
        after["address"] = detected_ip
        if args.ttl is not None:
            after["ttl"] = args.ttl

        if args.dry_run:
            logger.debug("Dry-run mode enabled; skipping record update call")
            return 0

        update_a_record(
            session=session,
            host=args.host,
            user=args.user,
            domain=args.domain,
            record=target,
            ttl=args.ttl,
            verify_ssl=verify_ssl,
            timeout=args.timeout,
            new_ip=detected_ip,
        )

        log_success(logger, before, after)
        return 0

    except requests.HTTPError as exc:
        body = exc.response.text[:1000] if exc.response is not None else str(exc)
        log_error(logger, f"HTTP error: {exc} | Response: {body}")
        return 2
    except (requests.RequestException, CpanelApiError, KeyError, ValueError) as exc:
        log_error(logger, str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
