"""Fail-closed Cloudflare routing audit and targeted redirect correction."""
from __future__ import annotations

from pathlib import Path
from copy import deepcopy
from io import BytesIO
import json
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from scripts import cloudflare_routing_audit as routing


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cloudflare_routing_audit.py"
WORKFLOW = ROOT / ".github" / "workflows" / "cloudflare-routing-audit.yml"
ACCOUNT_ID = "a" * 32
ZONE_ID = "b" * 32
ZONE_RULESET_ID = "c" * 32
ZONE_RULE_ID = "d" * 32
ACCOUNT_RULESET_ID = "e" * 32
ACCOUNT_RULE_ID = "f" * 32
LIST_ID = "1" * 32
ITEM_ID = "2" * 32
PAGE_RULE_ID = "3" * 32
NON_REDIRECT_PAGE_RULE_ID = "4" * 32


def single_redirect_rule(
    *,
    rule_id: str = ZONE_RULE_ID,
    expression: str = 'http.host contains "telacore.org"',
    target: str = "awsas.de/403-page.html",
    enabled: bool = True,
    status_code: int = 301,
) -> dict[str, object]:
    return {
        "id": rule_id,
        "version": "7",
        "action": "redirect",
        "action_parameters": {
            "from_value": {
                "status_code": status_code,
                "target_url": {"value": target},
                "preserve_query_string": False,
            }
        },
        "expression": expression,
        "description": "HTTP/1.1 protection",
        "last_updated": "2026-08-25T12:00:00Z",
        "ref": "protect_http_1_1",
        "enabled": enabled,
    }


def zone_ruleset(rules: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": ZONE_RULESET_ID,
        "name": "default",
        "kind": "zone",
        "phase": "http_request_dynamic_redirect",
        "rules": rules,
    }


def test_routing_audit_cli_has_bounded_help() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--apply" in completed.stdout
    assert "CLOUDFLARE_API_TOKEN" not in completed.stdout


def test_selects_only_one_active_zone_redirect_by_target() -> None:
    description_only = single_redirect_rule(
        rule_id="4" * 32,
        target="https://example.org/not-the-target",
    )
    description_only["description"] = "mentions awsas.de/403-page.html"
    disabled = single_redirect_rule(rule_id="5" * 32, enabled=False)

    selected = routing.select_zone_redirect_rule(
        zone_ruleset([description_only, disabled, single_redirect_rule()])
    )

    assert selected["id"] == ZONE_RULE_ID


@pytest.mark.parametrize(
    "rules",
    (
        [],
        [
            single_redirect_rule(rule_id="6" * 32),
            single_redirect_rule(rule_id="7" * 32),
        ],
    ),
    ids=("zero", "ambiguous"),
)
def test_zone_redirect_selection_fails_closed_unless_exactly_one_active_match(
    rules: list[dict[str, object]],
) -> None:
    with pytest.raises(routing.RoutingAuditError, match="exactly one active zone single redirect"):
        routing.select_zone_redirect_rule(zone_ruleset(rules))


def test_zone_redirect_selection_rejects_unexpected_rule_shape() -> None:
    malformed = single_redirect_rule()
    malformed["enabled"] = "yes"

    with pytest.raises(routing.RoutingAuditError, match="unexpected zone redirect rule"):
        routing.select_zone_redirect_rule(zone_ruleset([malformed]))


@pytest.mark.parametrize(
    "target_url",
    (
        {"value": "https://awsas.de/403-page.html"},
        {"value": "awsas.de/403-page.html?source=lookalike"},
        {"expression": 'concat("awsas.de/403-page.html", http.request.uri.path)'},
    ),
    ids=("absolute-url", "query-suffix", "dynamic-expression"),
)
def test_zone_redirect_selection_rejects_non_exact_target(
    target_url: dict[str, str],
) -> None:
    rule = single_redirect_rule()
    rule["action_parameters"]["from_value"]["target_url"] = target_url  # type: ignore[index]

    with pytest.raises(routing.RoutingAuditError, match="exactly one active zone single redirect"):
        routing.select_zone_redirect_rule(zone_ruleset([rule]))


@pytest.mark.parametrize(
    ("key", "value"),
    (("unknown_semantics", {"unsafe": True}), ("categories", ["redirect"])),
    ids=("unknown", "categories"),
)
def test_zone_redirect_selection_rejects_unpatchable_semantic_key(
    key: str,
    value: object,
) -> None:
    rule = single_redirect_rule()
    rule[key] = value

    with pytest.raises(routing.RoutingAuditError, match="unexpected zone redirect rule"):
        routing.select_zone_redirect_rule(zone_ruleset([rule]))


def test_zone_redirect_selection_rejects_non_301_redirect() -> None:
    rule = single_redirect_rule(status_code=302)

    with pytest.raises(routing.RoutingAuditError, match="exactly one active zone single redirect"):
        routing.select_zone_redirect_rule(zone_ruleset([rule]))


def test_patch_payload_wraps_exact_expression_and_preserves_rule_definition() -> None:
    rule = single_redirect_rule(expression='http.host eq "legacy.telacore.org"')

    payload = routing.build_patch_payload(rule)

    assert payload == {
        "action": "redirect",
        "action_parameters": rule["action_parameters"],
        "expression": (
            'not (http.host in {"staging.workers.desinfect.telacore.org" '
            '"production.workers.desinfect.telacore.org"}) and '
            '(http.host eq "legacy.telacore.org")'
        ),
        "description": "HTTP/1.1 protection",
        "ref": "protect_http_1_1",
        "enabled": True,
    }


def test_patch_payload_is_idempotent_for_its_exact_wrapper() -> None:
    rule = single_redirect_rule()
    first = routing.build_patch_payload(rule)
    assert first is not None
    rule["expression"] = first["expression"]

    assert routing.build_patch_payload(rule) is None


def test_patch_payload_rejects_dynamic_redirect_target() -> None:
    rule = single_redirect_rule()
    rule["action_parameters"]["from_value"]["target_url"] = {  # type: ignore[index]
        "expression": 'concat("https://awsas.de/403-page.html", http.request.uri.path)'
    }

    with pytest.raises(routing.RoutingAuditError, match="static target_url.value"):
        routing.build_patch_payload(rule)


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self, _: int = -1) -> bytes:
        return self._body

    def close(self) -> None:
        pass


class RecordingOpener:
    def __init__(self, responses: dict[tuple[str, str], object]) -> None:
        self.responses = responses
        self.requests: list[object] = []

    def __call__(self, request: object, timeout: float) -> FakeResponse:
        assert timeout == routing.REQUEST_TIMEOUT_SECONDS
        self.requests.append(request)
        method = request.get_method()  # type: ignore[attr-defined]
        parsed = urlsplit(request.full_url)  # type: ignore[attr-defined]
        key = (method, parsed.path)
        return FakeResponse(self.responses[key])


class SequentialOpener:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[object] = []

    def __call__(self, request: object, timeout: float) -> FakeResponse:
        assert timeout == routing.REQUEST_TIMEOUT_SECONDS
        self.requests.append(request)
        return FakeResponse(self.responses.pop(0))


def envelope(result: object, result_info: object | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "success": True,
        "errors": [],
        "messages": [],
        "result": result,
    }
    if result_info is not None:
        value["result_info"] = result_info
    return value


def test_client_sends_bounded_patch_payload_without_token_in_body_or_url() -> None:
    path = f"/zones/{ZONE_ID}/rulesets/{ZONE_RULESET_ID}/rules/{ZONE_RULE_ID}"
    payload = {"action": "redirect", "expression": "safe"}
    opener = RecordingOpener({("PATCH", f"/client/v4{path}"): envelope({"id": ZONE_RULESET_ID})})
    client = routing.CloudflareClient("top-secret-token", opener=opener)

    result = client.request("PATCH", path, payload=payload, operation="update-zone-rule")

    assert result["result"] == {"id": ZONE_RULESET_ID}
    request = opener.requests[0]
    assert json.loads(request.data) == payload  # type: ignore[attr-defined]
    assert request.get_method() == "PATCH"  # type: ignore[attr-defined]
    assert "top-secret-token" not in request.full_url  # type: ignore[attr-defined]
    assert b"top-secret-token" not in request.data  # type: ignore[attr-defined]
    assert request.get_header("Authorization") == "Bearer top-secret-token"  # type: ignore[attr-defined]


def test_client_redacts_http_error_body_and_token() -> None:
    secret = "top-secret-token"

    def denied(request: object, timeout: float) -> object:
        raise HTTPError(
            request.full_url,  # type: ignore[attr-defined]
            403,
            "body-secret-detail",
            {},
            BytesIO(b"body-secret-detail"),
        )

    client = routing.CloudflareClient(secret, opener=denied)
    with pytest.raises(routing.RoutingAuditError) as caught:
        client.request("GET", "/zones", operation="resolve-zone")

    assert str(caught.value) == "Cloudflare API failed: operation=resolve-zone http_status=403"
    assert secret not in str(caught.value)
    assert "body-secret-detail" not in str(caught.value)


def test_audit_checks_exact_zone_account_and_all_redirect_sources() -> None:
    zone_rule = single_redirect_rule()
    account_ruleset = {
        "id": ACCOUNT_RULESET_ID,
        "name": "default",
        "kind": "root",
        "phase": "http_request_redirect",
        "rules": [
            {
                "id": ACCOUNT_RULE_ID,
                "action": "redirect",
                "action_parameters": {
                    "from_list": {"name": "legacy_redirects", "key": "http.request.full_uri"}
                },
                "expression": "http.request.full_uri in $legacy_redirects",
                "enabled": True,
            }
        ],
    }
    responses = {
        ("GET", "/client/v4/zones"): envelope(
            [{"id": ZONE_ID, "name": "telacore.org", "status": "active", "account": {"id": ACCOUNT_ID}}],
            {"page": 1, "total_pages": 1},
        ),
        (
            "GET",
            f"/client/v4/zones/{ZONE_ID}/rulesets/phases/http_request_dynamic_redirect/entrypoint",
        ): envelope(zone_ruleset([zone_rule])),
        (
            "GET",
            f"/client/v4/accounts/{ACCOUNT_ID}/rulesets/phases/http_request_redirect/entrypoint",
        ): envelope(account_ruleset),
        ("GET", f"/client/v4/accounts/{ACCOUNT_ID}/rules/lists"): envelope(
            [{"id": LIST_ID, "name": "legacy_redirects", "kind": "redirect"}]
        ),
        (
            "GET",
            f"/client/v4/accounts/{ACCOUNT_ID}/rules/lists/{LIST_ID}/items",
        ): envelope(
            [
                {
                    "id": ITEM_ID,
                    "redirect": {
                        "source_url": "staging.workers.desinfect.telacore.org/healthz",
                        "target_url": "https://awsas.de/403-page.html",
                    },
                }
            ],
            {"cursors": {}},
        ),
        ("GET", f"/client/v4/zones/{ZONE_ID}/pagerules"): envelope(
            [
                {
                    "id": PAGE_RULE_ID,
                    "status": "active",
                    "targets": [
                        {
                            "target": "url",
                            "constraint": {"operator": "matches", "value": "*telacore.org/*"},
                        }
                    ],
                    "actions": [
                        {"id": "forwarding_url", "value": {"url": "https://awsas.de/403-page.html", "status_code": 301}}
                    ],
                },
                {
                    "id": NON_REDIRECT_PAGE_RULE_ID,
                    "status": "active",
                    "targets": [
                        {
                            "target": "url",
                            "constraint": {"operator": "matches", "value": "*telacore.org/*"},
                        }
                    ],
                    "actions": [{"id": "browser_check", "value": "on"}],
                },
            ]
        ),
    }
    opener = RecordingOpener(responses)

    report = routing.audit_routing(routing.CloudflareClient("secret", opener=opener), ACCOUNT_ID)

    assert report.zone_id == ZONE_ID
    assert routing.select_zone_redirect_rule(report.zone_ruleset)["id"] == ZONE_RULE_ID
    assert {finding.kind for finding in report.findings} == {
        "zone_single_redirect",
        "account_bulk_redirect_item",
        "legacy_page_rule",
    }
    assert [
        finding.object_id
        for finding in report.findings
        if finding.kind == "legacy_page_rule"
    ] == [PAGE_RULE_ID]
    zone_request = opener.requests[0]
    query = parse_qs(urlsplit(zone_request.full_url).query)  # type: ignore[attr-defined]
    assert query == {
        "account.id": [ACCOUNT_ID],
        "match": ["all"],
        "name": ["telacore.org"],
        "page": ["1"],
        "per_page": ["50"],
        "status": ["active"],
    }
    item_request = next(
        request
        for request in opener.requests
        if urlsplit(request.full_url).path.endswith(f"/rules/lists/{LIST_ID}/items")
    )
    assert parse_qs(urlsplit(item_request.full_url).query) == {"per_page": ["500"]}
    list_request = next(
        request
        for request in opener.requests
        if urlsplit(request.full_url).path.endswith(f"/accounts/{ACCOUNT_ID}/rules/lists")
    )
    assert urlsplit(list_request.full_url).query == ""
    page_rule_request = next(
        request
        for request in opener.requests
        if urlsplit(request.full_url).path.endswith(f"/zones/{ZONE_ID}/pagerules")
    )
    assert parse_qs(urlsplit(page_rule_request.full_url).query) == {"status": ["active"]}
    rendered = "\n".join(report.render())
    assert report.render()[0] == "routing_audit zone=telacore.org"
    assert ACCOUNT_ID not in rendered
    assert ZONE_ID not in rendered
    assert "secret" not in rendered
    assert "http.host contains" not in rendered
    assert "/healthz" not in rendered
    assert "403-page.html" not in rendered


def test_zone_resolution_rejects_wrong_account_even_with_right_zone_name() -> None:
    opener = RecordingOpener(
        {
            ("GET", "/client/v4/zones"): envelope(
                [{"id": ZONE_ID, "name": "telacore.org", "status": "active", "account": {"id": "9" * 32}}],
                {"page": 1, "total_pages": 1},
            )
        }
    )

    with pytest.raises(routing.RoutingAuditError, match="exact zone/account match"):
        routing.audit_routing(routing.CloudflareClient("secret", opener=opener), ACCOUNT_ID)


def test_apply_reloads_preimage_then_dry_runs_and_repeats_identical_patch() -> None:
    original = zone_ruleset([single_redirect_rule()])
    payload = routing.build_patch_payload(original["rules"][0])  # type: ignore[index]
    assert payload is not None
    updated = deepcopy(original)
    updated["rules"][0]["expression"] = payload["expression"]  # type: ignore[index]
    opener = SequentialOpener(
        [envelope(original), envelope(None), envelope(updated), envelope(updated)]
    )
    client = routing.CloudflareClient("secret", opener=opener)
    report = routing.AuditReport(
        ACCOUNT_ID,
        ZONE_ID,
        original,
        (
            routing.AuditFinding(
                "zone_single_redirect", ZONE_RULE_ID, True, ("awsas.de", "telacore")
            ),
            routing.AuditFinding(
                "zone_single_redirect", "9" * 32, False, ("telacore",)
            ),
        ),
    )

    assert routing.apply_verified_exception(client, report, original["rules"][0]) is True  # type: ignore[index]

    assert len(opener.requests) == 4
    urls = [request.full_url for request in opener.requests]  # type: ignore[attr-defined]
    assert urls[0].endswith(
        f"/zones/{ZONE_ID}/rulesets/phases/http_request_dynamic_redirect/entrypoint"
    )
    assert urls[1].endswith(
        f"/zones/{ZONE_ID}/rulesets/{ZONE_RULESET_ID}/rules/{ZONE_RULE_ID}?dry_run=true"
    )
    assert urls[2].endswith(
        f"/zones/{ZONE_ID}/rulesets/{ZONE_RULESET_ID}/rules/{ZONE_RULE_ID}"
    )
    assert urls[3] == urls[0]
    assert opener.requests[1].data == opener.requests[2].data  # type: ignore[attr-defined]


def test_apply_rejects_non_null_dry_run_result_before_real_patch() -> None:
    original = zone_ruleset([single_redirect_rule()])
    payload = routing.build_patch_payload(original["rules"][0])  # type: ignore[index]
    assert payload is not None
    unexpected = deepcopy(original)
    unexpected["rules"][0]["expression"] = payload["expression"]  # type: ignore[index]
    opener = SequentialOpener([envelope(original), envelope(unexpected)])
    report = routing.AuditReport(
        ACCOUNT_ID,
        ZONE_ID,
        original,
        (
            routing.AuditFinding(
                "zone_single_redirect", ZONE_RULE_ID, True, ("awsas.de", "telacore")
            ),
        ),
    )

    with pytest.raises(routing.RoutingAuditError, match="dry-run.*unexpected result"):
        routing.apply_verified_exception(
            routing.CloudflareClient("secret", opener=opener),
            report,
            original["rules"][0],  # type: ignore[index]
        )

    assert [request.get_method() for request in opener.requests] == ["GET", "PATCH"]  # type: ignore[attr-defined]
    assert opener.requests[1].full_url.endswith("?dry_run=true")  # type: ignore[attr-defined]


def test_apply_rejects_changed_preimage_before_any_patch() -> None:
    original = zone_ruleset([single_redirect_rule()])
    changed = deepcopy(original)
    changed["rules"][0]["expression"] = 'http.host eq "changed.telacore.org"'  # type: ignore[index]
    opener = SequentialOpener([envelope(changed)])
    report = routing.AuditReport(
        ACCOUNT_ID,
        ZONE_ID,
        original,
        (
            routing.AuditFinding(
                "zone_single_redirect", ZONE_RULE_ID, True, ("awsas.de", "telacore")
            ),
        ),
    )

    with pytest.raises(routing.RoutingAuditError, match="preimage changed"):
        routing.apply_verified_exception(
            routing.CloudflareClient("secret", opener=opener),
            report,
            original["rules"][0],  # type: ignore[index]
        )

    assert len(opener.requests) == 1
    assert opener.requests[0].get_method() == "GET"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "findings",
    (
        (routing.AuditFinding("account_bulk_redirect_item", "8" * 32, True, ("telacore",)),),
        (routing.AuditFinding("legacy_page_rule", "8" * 32, True, ("telacore",)),),
        (
            routing.AuditFinding(
                "zone_single_redirect", ZONE_RULE_ID, True, ("awsas.de", "telacore")
            ),
            routing.AuditFinding("zone_single_redirect", "8" * 32, True, ("telacore",)),
        ),
        (routing.AuditFinding("zone_single_redirect", "8" * 32, True, ("telacore",)),),
    ),
    ids=("bulk", "page-rule", "second-active-zone", "wrong-candidate-id"),
)
def test_apply_requires_exactly_one_active_candidate_finding(
    findings: tuple[routing.AuditFinding, ...],
) -> None:
    original = zone_ruleset([single_redirect_rule()])
    report = routing.AuditReport(ACCOUNT_ID, ZONE_ID, original, findings)

    with pytest.raises(routing.RoutingAuditError, match="exactly one active candidate finding"):
        routing.apply_verified_exception(
            routing.CloudflareClient("secret", opener=lambda *_args, **_kwargs: pytest.fail()),
            report,
            original["rules"][0],  # type: ignore[index]
        )


def test_workflow_is_manual_main_only_staging_audit_with_explicit_apply() -> None:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = data.get("on", data.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    apply_input = triggers["workflow_dispatch"]["inputs"]["apply"]
    assert apply_input == {
        "description": "Apply verified worker-host exclusion to the one matching zone redirect",
        "required": True,
        "type": "boolean",
        "default": False,
    }
    assert data["permissions"] == {"contents": "read"}
    assert set(data["jobs"]) == {"audit_routing"}
    job = data["jobs"]["audit_routing"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == {"name": "cloudflare-watchdog-staging"}
    assert "production" not in json.dumps(data).lower()
    steps = job["steps"]
    audit = next(step for step in steps if step["name"] == "Audit routing without mutation")
    apply = next(step for step in steps if step["name"] == "Apply one verified zone redirect correction")
    assert audit["if"] == "!inputs.apply"
    assert audit["run"] == "python3 scripts/cloudflare_routing_audit.py"
    assert apply["if"] == "inputs.apply"
    assert apply["run"] == "python3 scripts/cloudflare_routing_audit.py --apply"
