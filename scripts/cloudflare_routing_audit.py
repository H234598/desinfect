#!/usr/bin/env python3
"""Audit Cloudflare redirects and optionally correct one verified zone redirect."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
import re
import sys
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_ROOT = "https://api.cloudflare.com/client/v4"
ZONE_NAME = "telacore.org"
ZONE_PHASE = "http_request_dynamic_redirect"
ACCOUNT_PHASE = "http_request_redirect"
TARGET_FRAGMENT = "awsas.de/403-page.html"
WORKER_HOSTS = (
    "staging.workers.desinfect.telacore.org",
    "production.workers.desinfect.telacore.org",
)
HOST_EXCLUSION = (
    'not (http.host in {"staging.workers.desinfect.telacore.org" '
    '"production.workers.desinfect.telacore.org"})'
)
WRAPPER_PREFIX = f"{HOST_EXCLUSION} and ("
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
LIST_NAME_PATTERN = re.compile(r"^[a-z0-9_]{1,50}$")
PATCH_FIELDS = (
    "action",
    "action_parameters",
    "expression",
    "description",
    "enabled",
    "ref",
)
ZONE_RULE_KEYS = {*PATCH_FIELDS, "id", "version", "last_updated", "categories"}


class RoutingAuditError(RuntimeError):
    """Safe, redacted routing audit failure."""


@dataclass(frozen=True)
class AuditFinding:
    kind: str
    object_id: str
    active: bool
    signals: tuple[str, ...]
    metadata: tuple[str, ...] = ()

    def render(self) -> str:
        fields = (
            f"type={self.kind}",
            f"id={self.object_id}",
            f"active={str(self.active).lower()}",
            f"signals={','.join(self.signals)}",
            *self.metadata,
        )
        return "audit_match " + " ".join(fields)


@dataclass(frozen=True)
class AuditReport:
    account_id: str
    zone_id: str
    zone_ruleset: dict[str, object] | None
    findings: tuple[AuditFinding, ...]

    def render(self) -> list[str]:
        return [
            f"routing_audit zone={ZONE_NAME}",
            *(finding.render() for finding in self.findings),
            f"audit_summary relevant_matches={len(self.findings)}",
        ]


class CloudflareClient:
    """Small stdlib-only Cloudflare JSON client with redacted failures."""

    def __init__(self, token: str, *, opener: Callable[..., object] = urlopen) -> None:
        if not token or token.isspace():
            raise RoutingAuditError("Cloudflare API token is missing")
        self._token = token
        self._opener = opener

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        operation: str,
        optional_404: bool = False,
    ) -> dict[str, object] | None:
        if method not in {"GET", "PATCH"} or not path.startswith("/"):
            raise RoutingAuditError("unexpected Cloudflare API request")
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "desinfect-cloudflare-routing-audit/1",
        }
        if payload is not None:
            if method != "PATCH":
                raise RoutingAuditError("unexpected Cloudflare API payload")
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = Request(API_ROOT + path, data=data, headers=headers, method=method)
        response: object | None = None
        try:
            response = self._opener(request, timeout=REQUEST_TIMEOUT_SECONDS)
            status = getattr(response, "status", None)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise RoutingAuditError(
                    f"Cloudflare API failed: operation={operation} http_status=unexpected"
                )
            body = response.read(MAX_RESPONSE_BYTES + 1)  # type: ignore[attr-defined]
        except HTTPError as exc:
            exc.close()
            if optional_404 and exc.code == 404:
                return None
            raise RoutingAuditError(
                f"Cloudflare API failed: operation={operation} http_status={exc.code}"
            ) from None
        except RoutingAuditError:
            raise
        except (OSError, TimeoutError, URLError):
            raise RoutingAuditError(
                f"Cloudflare API failed: operation={operation} category=network"
            ) from None
        finally:
            if response is not None:
                response.close()  # type: ignore[attr-defined]
        if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
            raise RoutingAuditError(
                f"Cloudflare API failed: operation={operation} category=response-size"
            )
        try:
            envelope = json.loads(body.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RoutingAuditError(
                f"Cloudflare API failed: operation={operation} category=json"
            ) from None
        if (
            not isinstance(envelope, dict)
            or envelope.get("success") is not True
            or envelope.get("errors") != []
            or "result" not in envelope
        ):
            raise RoutingAuditError(
                f"Cloudflare API failed: operation={operation} category=envelope"
            )
        return envelope


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise RoutingAuditError(f"unexpected {label}")
    return value


def _result(envelope: dict[str, object] | None, label: str) -> object:
    if envelope is None:
        raise RoutingAuditError(f"missing {label}")
    return envelope["result"]


def _query(path: str, values: dict[str, object]) -> str:
    return f"{path}?{urlencode(values)}"


def _cursor_results(
    client: CloudflareClient,
    path: str,
    operation: str,
) -> list[object]:
    items: list[object] = []
    marker: str | None = None
    seen: set[str] = set()
    while True:
        params: dict[str, object] = {"per_page": 500}
        if marker is not None:
            params["cursor"] = marker
        envelope = client.request("GET", _query(path, params), operation=operation)
        result = _result(envelope, operation)
        info = envelope.get("result_info") if envelope else None
        cursors = info.get("cursors") if isinstance(info, dict) else None
        if not isinstance(result, list) or not isinstance(cursors, dict):
            raise RoutingAuditError(f"unexpected {operation} response")
        items.extend(result)
        after = cursors.get("after")
        if after is None:
            return items
        if not isinstance(after, str) or not after or after in seen:
            raise RoutingAuditError(f"unexpected {operation} pagination")
        seen.add(after)
        marker = after


def _zone_rule(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not set(value) <= ZONE_RULE_KEYS:
        raise RoutingAuditError("unexpected zone redirect rule")
    try:
        rule_id = value["id"]
        action = value["action"]
        parameters = value["action_parameters"]
        expression = value["expression"]
        description = value["description"]
        enabled = value["enabled"]
        ref = value["ref"]
    except KeyError:
        raise RoutingAuditError("unexpected zone redirect rule") from None
    if (
        action != "redirect"
        or not isinstance(parameters, dict)
        or not isinstance(expression, str)
        or not expression
        or not isinstance(description, str)
        or not isinstance(enabled, bool)
        or not isinstance(ref, str)
    ):
        raise RoutingAuditError("unexpected zone redirect rule")
    _id(rule_id, "zone redirect rule id")
    from_value = parameters.get("from_value")
    target = from_value.get("target_url") if isinstance(from_value, dict) else None
    if not isinstance(target, dict) or set(target) not in ({"value"}, {"expression"}):
        raise RoutingAuditError("unexpected zone redirect rule")
    if not isinstance(next(iter(target.values())), str) or not next(iter(target.values())):
        raise RoutingAuditError("unexpected zone redirect rule")
    if "categories" in value and (
        not isinstance(value["categories"], list)
        or not all(isinstance(category, str) for category in value["categories"])
    ):
        raise RoutingAuditError("unexpected zone redirect rule")
    return value


def _zone_ruleset(value: object) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or value.get("kind") != "zone"
        or value.get("phase") != ZONE_PHASE
        or not isinstance(value.get("rules"), list)
    ):
        raise RoutingAuditError("unexpected zone redirect ruleset")
    _id(value.get("id"), "zone redirect ruleset id")
    for rule in value["rules"]:
        _zone_rule(rule)
    return value


def _target(rule: dict[str, object]) -> str:
    parameters = rule["action_parameters"]
    from_value = parameters["from_value"]  # type: ignore[index]
    target = from_value["target_url"]  # type: ignore[index]
    return next(iter(target.values()))  # type: ignore[union-attr,return-value]


def select_zone_redirect_rule(ruleset: object) -> dict[str, object]:
    rules = _zone_ruleset(ruleset)["rules"]
    matches = [
        rule
        for rule in rules  # type: ignore[union-attr]
        if rule["enabled"] is True
        and rule["action_parameters"]["from_value"]["target_url"]  # type: ignore[index]
        == {"value": TARGET_FRAGMENT}
    ]
    if len(matches) != 1:
        raise RoutingAuditError("expected exactly one active zone single redirect match")
    return matches[0]


def build_patch_payload(rule: object) -> dict[str, object] | None:
    validated = _zone_rule(rule)
    parameters = validated["action_parameters"]
    target = parameters["from_value"]["target_url"]  # type: ignore[index]
    if set(target) != {"value"}:  # type: ignore[arg-type]
        raise RoutingAuditError("apply requires static target_url.value")
    expression = validated["expression"]
    if expression.startswith(WRAPPER_PREFIX) and expression.endswith(")"):  # type: ignore[union-attr]
        return None
    payload = {field: deepcopy(validated[field]) for field in PATCH_FIELDS}
    payload["expression"] = f"{WRAPPER_PREFIX}{expression})"
    return payload


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _strings(nested)


def _signals(*values: object) -> tuple[str, ...]:
    combined = "\n".join(text.lower() for value in values for text in _strings(value))
    checks = (
        ("awsas.de", "awsas.de"),
        (WORKER_HOSTS[0], "staging-worker"),
        (WORKER_HOSTS[1], "production-worker"),
        ("telacore", "telacore"),
    )
    return tuple(label for needle, label in checks if needle in combined)


def _resolve_zone(client: CloudflareClient, account_id: str) -> str:
    params = {
        "account.id": account_id,
        "match": "all",
        "name": ZONE_NAME,
        "page": 1,
        "per_page": 50,
        "status": "active",
    }
    envelope = client.request("GET", _query("/zones", params), operation="resolve-zone")
    result = _result(envelope, "zone lookup")
    info = envelope.get("result_info") if envelope else None
    if not isinstance(result, list) or len(result) != 1 or not isinstance(info, dict):
        raise RoutingAuditError("expected exact zone/account match")
    zone = result[0]
    account = zone.get("account") if isinstance(zone, dict) else None
    if (
        not isinstance(zone, dict)
        or zone.get("name") != ZONE_NAME
        or zone.get("status") != "active"
        or info.get("page") != 1
        or info.get("total_pages") != 1
        or not isinstance(account, dict)
        or account.get("id") != account_id
    ):
        raise RoutingAuditError("expected exact zone/account match")
    return _id(zone.get("id"), "zone id")


def _read_entrypoint(
    client: CloudflareClient,
    scope: str,
    scope_id: str,
    phase: str,
    operation: str,
) -> object | None:
    path = f"/{scope}/{scope_id}/rulesets/phases/{phase}/entrypoint"
    envelope = client.request("GET", path, operation=operation, optional_404=True)
    return None if envelope is None else _result(envelope, operation)


def _zone_audit(
    client: CloudflareClient, zone_id: str
) -> tuple[dict[str, object] | None, list[AuditFinding]]:
    value = _read_entrypoint(
        client, "zones", zone_id, ZONE_PHASE, "read-zone-single-redirects"
    )
    if value is None:
        return None, []
    ruleset = _zone_ruleset(value)
    findings = []
    for rule in ruleset["rules"]:  # type: ignore[union-attr]
        signals = _signals(rule["expression"], _target(rule))
        if signals:
            expression = rule["expression"]
            digest = hashlib.sha256(expression.encode()).hexdigest()[:12]
            findings.append(
                AuditFinding(
                    "zone_single_redirect",
                    rule["id"],
                    rule["enabled"],
                    signals,
                    (f"expression_sha256={digest}", f"expression_length={len(expression)}"),
                )
            )
    return ruleset, findings


def _bulk_audit(client: CloudflareClient, account_id: str) -> list[AuditFinding]:
    ruleset = _read_entrypoint(
        client, "accounts", account_id, ACCOUNT_PHASE, "read-account-bulk-redirects"
    )
    if ruleset is None:
        return []
    if (
        not isinstance(ruleset, dict)
        or ruleset.get("kind") != "root"
        or ruleset.get("phase") != ACCOUNT_PHASE
        or not isinstance(ruleset.get("rules"), list)
    ):
        raise RoutingAuditError("unexpected account redirect ruleset")
    _id(ruleset.get("id"), "account redirect ruleset id")
    findings: list[AuditFinding] = []
    active_lists: list[tuple[str, str]] = []
    for rule in ruleset["rules"]:
        parameters = rule.get("action_parameters") if isinstance(rule, dict) else None
        from_list = parameters.get("from_list") if isinstance(parameters, dict) else None
        if (
            not isinstance(rule, dict)
            or rule.get("action") != "redirect"
            or not isinstance(from_list, dict)
            or not isinstance(rule.get("expression"), str)
            or not isinstance(rule.get("enabled"), bool)
            or not isinstance(from_list.get("name"), str)
        ):
            raise RoutingAuditError("unexpected account redirect rule")
        rule_id = _id(rule.get("id"), "account redirect rule id")
        name = from_list["name"]
        if not LIST_NAME_PATTERN.fullmatch(name):
            raise RoutingAuditError("unexpected account redirect list name")
        signals = _signals(rule["expression"])
        if rule["enabled"] and signals:
            digest = hashlib.sha256(rule["expression"].encode()).hexdigest()[:12]
            findings.append(
                AuditFinding(
                    "account_bulk_redirect_rule",
                    rule_id,
                    True,
                    signals,
                    (f"expression_sha256={digest}",),
                )
            )
        if rule["enabled"]:
            active_lists.append((rule_id, name))
    if not active_lists:
        return findings
    lists_envelope = client.request(
        "GET",
        f"/accounts/{account_id}/rules/lists",
        operation="read-account-redirect-lists",
    )
    values = _result(lists_envelope, "read-account-redirect-lists")
    if not isinstance(values, list):
        raise RoutingAuditError("unexpected read-account-redirect-lists response")
    lists: dict[str, str] = {}
    for value in values:
        if not isinstance(value, dict) or value.get("kind") != "redirect":
            continue
        name = value.get("name")
        if not isinstance(name, str) or name in lists or not LIST_NAME_PATTERN.fullmatch(name):
            raise RoutingAuditError("unexpected account redirect list")
        lists[name] = _id(value.get("id"), "account redirect list id")
    for rule_id, name in active_lists:
        if name not in lists:
            raise RoutingAuditError("active account redirect list not found")
        list_id = lists[name]
        items = _cursor_results(
            client,
            f"/accounts/{account_id}/rules/lists/{list_id}/items",
            "read-account-redirect-list-items",
        )
        for item in items:
            redirect = item.get("redirect") if isinstance(item, dict) else None
            if not isinstance(item, dict) or not isinstance(redirect, dict):
                raise RoutingAuditError("unexpected account redirect list item")
            source, target = redirect.get("source_url"), redirect.get("target_url")
            if not isinstance(source, str) or not isinstance(target, str):
                raise RoutingAuditError("unexpected account redirect list item")
            signals = _signals(source, target)
            if signals:
                findings.append(
                    AuditFinding(
                        "account_bulk_redirect_item",
                        _id(item.get("id"), "account redirect list item id"),
                        True,
                        signals,
                        (f"rule_id={rule_id}", f"list_id={list_id}"),
                    )
                )
    return findings


def _page_rule_audit(client: CloudflareClient, zone_id: str) -> list[AuditFinding]:
    envelope = client.request(
        "GET",
        _query(f"/zones/{zone_id}/pagerules", {"status": "active"}),
        operation="read-legacy-page-rules",
    )
    values = _result(envelope, "read-legacy-page-rules")
    if not isinstance(values, list):
        raise RoutingAuditError("unexpected read-legacy-page-rules response")
    findings = []
    for rule in values:
        if (
            not isinstance(rule, dict)
            or rule.get("status") != "active"
            or not isinstance(rule.get("targets"), list)
            or not isinstance(rule.get("actions"), list)
        ):
            raise RoutingAuditError("unexpected legacy page rule")
        forwarding = [
            action
            for action in rule["actions"]
            if isinstance(action, dict) and action.get("id") == "forwarding_url"
        ]
        if not forwarding:
            continue
        signals = _signals(rule["targets"], forwarding)
        if not signals:
            continue
        action_ids = [
            action["id"]
            for action in rule["actions"]
            if isinstance(action, dict)
            and isinstance(action.get("id"), str)
            and re.fullmatch(r"[a-z0-9_-]{1,40}", action["id"])
        ]
        findings.append(
            AuditFinding(
                "legacy_page_rule",
                _id(rule.get("id"), "legacy page rule id"),
                True,
                signals,
                (f"actions={','.join(action_ids[:3]) or 'unknown'}",),
            )
        )
    return findings


def audit_routing(client: CloudflareClient, account_id: str) -> AuditReport:
    account_id = _id(account_id, "account id")
    zone_id = _resolve_zone(client, account_id)
    ruleset, findings = _zone_audit(client, zone_id)
    findings.extend(_bulk_audit(client, account_id))
    findings.extend(_page_rule_audit(client, zone_id))
    return AuditReport(account_id, zone_id, ruleset, tuple(findings))


def _rule_semantics(rule: dict[str, object]) -> dict[str, object]:
    return {field: deepcopy(rule[field]) for field in PATCH_FIELDS}


def _ruleset_semantics(ruleset: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    return [
        (rule["id"], _rule_semantics(rule))  # type: ignore[arg-type]
        for rule in ruleset["rules"]  # type: ignore[union-attr]
    ]


def _verify_update(
    before: dict[str, object],
    after_value: object,
    rule_id: str,
    payload: dict[str, object],
    failure: str,
) -> None:
    after = _zone_ruleset(after_value)
    expected = _ruleset_semantics(before)
    for index, (candidate_id, semantics) in enumerate(expected):
        if candidate_id == rule_id:
            expected[index] = (candidate_id, {**semantics, "expression": payload["expression"]})
    if after["id"] != before["id"] or _ruleset_semantics(after) != expected:
        raise RoutingAuditError(failure)


def apply_verified_exception(
    client: CloudflareClient,
    report: AuditReport,
    rule: dict[str, object],
) -> bool:
    if any(finding.kind != "zone_single_redirect" for finding in report.findings):
        raise RoutingAuditError("non-zone redirect audit match blocks apply")
    if report.zone_ruleset is None:
        raise RoutingAuditError("expected exactly one active zone single redirect match")
    audited = _zone_ruleset(report.zone_ruleset)
    audited_rule = select_zone_redirect_rule(audited)
    if rule["id"] != audited_rule["id"] or _rule_semantics(rule) != _rule_semantics(audited_rule):
        raise RoutingAuditError("audited zone redirect preimage changed")
    payload = build_patch_payload(rule)
    if payload is None:
        return False
    rule_id = _id(rule.get("id"), "zone redirect rule id")
    ruleset_id = _id(audited.get("id"), "zone redirect ruleset id")
    entrypoint = f"/zones/{report.zone_id}/rulesets/phases/{ZONE_PHASE}/entrypoint"
    preflight_envelope = client.request("GET", entrypoint, operation="reload-zone-single-redirect")
    preflight = _zone_ruleset(_result(preflight_envelope, "reloaded zone redirect ruleset"))
    if preflight["id"] != ruleset_id or _ruleset_semantics(preflight) != _ruleset_semantics(audited):
        raise RoutingAuditError("audited zone redirect preimage changed")
    path = f"/zones/{report.zone_id}/rulesets/{ruleset_id}/rules/{rule_id}"
    dry_run = client.request(
        "PATCH",
        f"{path}?dry_run=true",
        payload=payload,
        operation="dry-run-zone-single-redirect",
    )
    if _result(dry_run, "dry-run zone redirect") is not None:
        raise RoutingAuditError("dry-run zone redirect returned unexpected result")
    updated = client.request(
        "PATCH", path, payload=payload, operation="update-zone-single-redirect"
    )
    _verify_update(
        preflight,
        _result(updated, "updated zone redirect ruleset"),
        rule_id,
        payload,
        "updated zone redirect verification failed",
    )
    live = client.request("GET", entrypoint, operation="verify-zone-single-redirect")
    _verify_update(
        preflight,
        _result(live, "live zone redirect ruleset"),
        rule_id,
        payload,
        "live zone redirect verification failed",
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the verified worker-host exception after the read-only audit",
    )
    args = parser.parse_args(argv)
    try:
        client = CloudflareClient(os.environ.get("CLOUDFLARE_API_TOKEN", ""))
        report = audit_routing(client, os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""))
        for line in report.render():
            print(line)
        if report.zone_ruleset is None:
            raise RoutingAuditError("expected exactly one active zone single redirect match")
        rule = select_zone_redirect_rule(report.zone_ruleset)
        build_patch_payload(rule)  # Validate static preimage during read-only audit too.
        if args.apply:
            changed = apply_verified_exception(client, report, rule)
            print("apply_result=updated" if changed else "apply_result=unchanged_already_wrapped")
        elif any(finding.kind != "zone_single_redirect" for finding in report.findings):
            print("audit_result=blocked_by_non_zone_redirect")
        else:
            print("audit_result=ready_for_explicit_apply")
    except RoutingAuditError as exc:
        print(f"routing audit failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
