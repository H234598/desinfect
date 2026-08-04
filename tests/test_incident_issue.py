from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from scripts.rki_pipeline.incident_issue import (
    DEFAULT_THRESHOLD,
    LABEL,
    MARKER,
    MAX_PAGES,
    REPOSITORY,
    TITLE_PREFIX,
    GitHubRestClient,
    IncidentIssueError,
    IncidentIssuePlan,
    IssueMatch,
    main as incident_main,
    plan_incident_issue,
)
from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.runtime_status import new_run, update_run


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "github_pat_abcdefghijklmnopqrstuvwxyz"
ACTIONS_TOKEN = "ghs_actions_read_only_abcdefghijklmnopqrstuvwxyz"


def public_status(
    *,
    failures: int = 0,
    state: str = "operational",
    message: str = "nicht gemeldet",
) -> dict[str, object]:
    value = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    value["updated_at"] = "2026-08-04T12:00:00Z"
    value["status"] = state
    value["pipeline"]["consecutive_failures"] = failures
    value["pipeline"]["last_error"] = (
        None
        if failures == 0
        else {
            "code": "pipeline_failed",
            "phase": "apply",
            "occurred_at": "2026-08-04T12:00:00Z",
            "message": message,
            "redacted": True,
        }
    )
    return value


def final_manifest(status: str) -> dict[str, object]:
    run = new_run(
        workflow="rki-pipeline",
        trigger_source="schedule",
        run_mode="apply",
        storage_backend="lfs",
        run_id=f"incident-{status}",
        now="2026-08-04T12:01:00Z",
    )
    run = update_run(
        run,
        expected_revision=1,
        status="running",
        phase="apply",
        now="2026-08-04T12:02:00Z",
    )
    return update_run(
        run,
        expected_revision=2,
        status=status,
        phase="apply",
        now="2026-08-04T12:03:00Z",
        error=(
            None
            if status == "success"
            else {
                "class": "unknown",
                "code": "manifest_failed",
                "message": "manifest failure",
                "retryable": True,
            }
        ),
    )


class FakeResponse:
    def __init__(self, value: object, *, status: int = 200) -> None:
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.status = status

    def read(self, amount: int) -> bytes:
        return self.raw[:amount]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[object, float]] = []

    def __call__(self, request: object, *, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, FakeResponse)
        return response


def marker_issue(number: int, state: str = "open") -> dict[str, object]:
    return {
        "body": f"{MARKER}\nexisting",
        "labels": [],
        "number": number,
        "state": state,
        "title": TITLE_PREFIX,
    }


def plan_fields(action: str) -> dict[str, object]:
    values: dict[str, object] = {
        "action": action,
        "repository": REPOSITORY,
        "labels": (LABEL,),
        "title": None,
        "body": None,
        "comment": None,
        "issue_number": None,
    }
    if action == "create":
        values.update(title=TITLE_PREFIX, body=f"{MARKER}\nbody")
    elif action in {"update", "reopen"}:
        values.update(
            title=TITLE_PREFIX,
            body=f"{MARKER}\nbody",
            issue_number=7,
        )
    elif action == "heal":
        values.update(comment="Pipeline wiederhergestellt.", issue_number=7)
    return values


@pytest.mark.parametrize(
    ("action", "overrides"),
    [
        ("create", {"action": "delete"}),
        ("create", {"repository": "attacker/example"}),
        ("create", {"labels": ()}),
        ("create", {"labels": [LABEL]}),
        ("create", {"title": "forged title"}),
        ("create", {"body": None}),
        ("create", {"body": "missing marker"}),
        ("create", {"body": f"{MARKER}\n{MARKER}"}),
        ("create", {"body": MARKER + "x" * 20_001}),
        ("create", {"comment": "forged comment"}),
        ("create", {"issue_number": 7}),
        ("update", {"issue_number": 0}),
        ("update", {"issue_number": True}),
        ("update", {"title": None}),
        ("update", {"body": None}),
        ("update", {"comment": "forged comment"}),
        ("heal", {"title": TITLE_PREFIX}),
        ("heal", {"body": MARKER}),
        ("heal", {"comment": None}),
        ("heal", {"comment": "x" * 2_001}),
        ("heal", {"issue_number": None}),
        ("noop", {"title": TITLE_PREFIX}),
        ("noop", {"body": MARKER}),
        ("noop", {"comment": "forged comment"}),
        ("noop", {"issue_number": False}),
    ],
)
def test_incident_plan_rejects_forged_action_fields(
    action: str,
    overrides: dict[str, object],
) -> None:
    values = plan_fields(action)
    values.update(overrides)

    with pytest.raises(IncidentIssueError, match="Plan"):
        IncidentIssuePlan(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "attacker/example"),
        ("labels", ("attacker-label",)),
        ("title", "forged title"),
        ("body", "missing marker"),
        ("issue_number", True),
    ],
)
def test_client_revalidates_frozen_plan_before_transport(
    field: str,
    value: object,
) -> None:
    status = public_status(failures=2, state="degraded")
    matches = (IssueMatch(number=7, state="open"),)
    plan = plan_incident_issue(status, matches)
    object.__setattr__(plan, field, value)
    transport = FakeTransport([])

    with pytest.raises(IncidentIssueError, match="Plan"):
        GitHubRestClient(TOKEN, transport=transport).apply(
            plan,
            status=status,
            matches=matches,
            threshold=DEFAULT_THRESHOLD,
        )

    assert transport.calls == []


@pytest.mark.parametrize("action", ["create", "update", "reopen"])
def test_client_rejects_valid_shaped_forged_body_before_transport(action: str) -> None:
    status = public_status(failures=2, state="degraded")
    matches = (
        ()
        if action == "create"
        else (IssueMatch(number=7, state="open" if action == "update" else "closed"),)
    )
    plan = plan_incident_issue(status, matches)
    object.__setattr__(plan, "body", f"{MARKER}\nsecret=forged-but-bounded")
    transport = FakeTransport([])

    with pytest.raises(IncidentIssueError, match="neu berechnet"):
        GitHubRestClient(TOKEN, transport=transport).apply(
            plan,
            status=status,
            matches=matches,
            threshold=DEFAULT_THRESHOLD,
        )

    assert transport.calls == []


def test_client_rejects_bounded_arbitrary_heal_comment_before_transport() -> None:
    status = public_status()
    matches = (IssueMatch(number=7, state="open"),)
    plan = plan_incident_issue(status, matches)
    object.__setattr__(plan, "comment", "bounded forged healing comment")
    transport = FakeTransport([])

    with pytest.raises(IncidentIssueError, match="neu berechnet"):
        GitHubRestClient(TOKEN, transport=transport).apply(
            plan,
            status=status,
            matches=matches,
            threshold=DEFAULT_THRESHOLD,
        )

    assert transport.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("action", "reopen"), ("issue_number", 8)],
)
def test_client_rejects_plan_different_from_replanned_matches_before_transport(
    field: str,
    value: object,
) -> None:
    status = public_status(failures=2, state="degraded")
    matches = (IssueMatch(number=7, state="open"),)
    plan = plan_incident_issue(status, matches)
    object.__setattr__(plan, field, value)
    transport = FakeTransport([])

    with pytest.raises(IncidentIssueError, match="neu berechnet"):
        GitHubRestClient(TOKEN, transport=transport).apply(
            plan,
            status=status,
            matches=matches,
            threshold=DEFAULT_THRESHOLD,
        )

    assert transport.calls == []


def test_create_plan_uses_fixed_identity_and_redacted_deterministic_body() -> None:
    message = (
        "Authorization: Bearer ghp_abcdefghijklmnop\n"
        "mail=operator@example.org [boom]* <tag> "
        "https://user:password@example.org/path?token=secret"
    )
    value = public_status(failures=DEFAULT_THRESHOLD, state="degraded", message=message)

    first = plan_incident_issue(value, ())
    second = plan_incident_issue(deepcopy(value), ())

    assert first == second
    assert first.action == "create"
    assert first.issue_number is None
    assert first.repository == REPOSITORY == "H234598/desinfect"
    assert first.labels == (LABEL,) == ("pipeline-incident",)
    assert first.title == TITLE_PREFIX == "[RKI-Pipeline] Wiederholter Fehler"
    assert first.body is not None
    assert first.body.count(MARKER) == 1
    assert r"pipeline\_failed" in first.body
    assert "ghp_abcdefghijklmnop" not in first.body
    assert "operator@example.org" not in first.body
    assert "user:password" not in first.body
    assert "\x1b" not in first.body
    assert "[boom]*" not in first.body


def test_markdown_sanitizes_ansi_before_redacting_split_token_and_escapes_tilde() -> None:
    message = "token=ghp_abcd\x1b[31mefghijklmnop\x1b[0m ~literal~"

    plan = plan_incident_issue(
        public_status(failures=DEFAULT_THRESHOLD, state="degraded", message=message),
        (),
    )

    assert plan.body is not None
    assert "ghp_abcdefghijklmnop" not in plan.body
    assert "ghp_" not in plan.body
    assert "\x1b" not in plan.body
    assert r"\~literal\~" in plan.body


def test_markdown_sanitizes_control_before_redacting_split_token() -> None:
    message = "token=ghp_abcd\x08efghijklmnop"

    plan = plan_incident_issue(
        public_status(failures=DEFAULT_THRESHOLD, state="degraded", message=message),
        (),
    )

    assert plan.body is not None
    assert "ghp_" not in plan.body
    assert "\x08" not in plan.body


@pytest.mark.parametrize("threshold", [1, 101, True])
def test_threshold_outside_bounded_range_is_rejected(threshold: object) -> None:
    with pytest.raises(IncidentIssueError, match="threshold"):
        plan_incident_issue(public_status(failures=2, state="degraded"), (), threshold=threshold)


@pytest.mark.parametrize(
    ("match", "expected_action"),
    [
        (IssueMatch(number=7, state="open"), "update"),
        (IssueMatch(number=7, state="closed"), "reopen"),
    ],
)
def test_threshold_plan_updates_or_reopens_single_issue(
    match: IssueMatch,
    expected_action: str,
) -> None:
    plan = plan_incident_issue(
        public_status(failures=2, state="degraded"),
        (match,),
    )

    assert plan.action == expected_action
    assert plan.issue_number == 7
    assert plan.body is not None


def test_healed_status_comments_and_closes_open_issue() -> None:
    plan = plan_incident_issue(
        public_status(),
        (IssueMatch(number=9, state="open"),),
    )

    assert plan.action == "heal"
    assert plan.issue_number == 9
    assert plan.body is None
    assert plan.comment is not None
    assert "wiederhergestellt" in plan.comment


@pytest.mark.parametrize(
    ("status", "matches"),
    [
        (public_status(failures=1, state="degraded"), ()),
        (public_status(), (IssueMatch(number=3, state="closed"),)),
    ],
)
def test_non_actionable_status_has_noop_plan(
    status: dict[str, object],
    matches: tuple[IssueMatch, ...],
) -> None:
    plan = plan_incident_issue(status, matches)

    assert plan.action == "noop"
    assert plan.body is None
    assert plan.comment is None


def test_duplicate_marker_matches_fail_closed() -> None:
    matches = (
        IssueMatch(number=3, state="open"),
        IssueMatch(number=4, state="closed"),
    )

    with pytest.raises(IncidentIssueError, match="Marker-Treffer"):
        plan_incident_issue(public_status(failures=2, state="degraded"), matches)


def test_invalid_public_status_is_rejected() -> None:
    value = public_status(failures=2, state="degraded")
    value["repository"] = "attacker/example"

    with pytest.raises(IncidentIssueError, match="Status"):
        plan_incident_issue(value, ())


def test_client_lists_marker_with_fixed_route_headers_timeout_and_pagination() -> None:
    first_page = [
        {"body": "ordinary", "number": number, "state": "open", "title": "ordinary"}
        for number in range(1, 101)
    ]
    transport = FakeTransport(
        [FakeResponse(first_page), FakeResponse([marker_issue(101)])]
    )
    client = GitHubRestClient(TOKEN, transport=transport)

    matches = client.list_issue_matches()

    assert matches == (IssueMatch(number=101, state="open"),)
    assert len(transport.calls) == 2
    for page, (request, timeout) in enumerate(transport.calls, start=1):
        assert request.full_url == (
            "https://api.github.com/repos/H234598/desinfect/issues"
            f"?state=all&per_page=100&page={page}"
        )
        assert request.method == "GET"
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        assert request.get_header("Accept") == "application/vnd.github+json"
        assert request.get_header("X-github-api-version") == "2022-11-28"
        assert timeout == 10.0


def test_client_fails_closed_at_pagination_bound() -> None:
    full_page = [
        {"body": "ordinary", "number": number, "state": "open", "title": "ordinary"}
        for number in range(1, 101)
    ]
    transport = FakeTransport([FakeResponse(full_page) for _ in range(MAX_PAGES)])

    with pytest.raises(IncidentIssueError, match="Seitengrenze"):
        GitHubRestClient(TOKEN, transport=transport).list_issue_matches()

    assert len(transport.calls) == MAX_PAGES


def test_client_rejects_oversized_response() -> None:
    transport = FakeTransport([FakeResponse(b"x" * 1_048_577)])

    with pytest.raises(IncidentIssueError, match="Antwortgröße"):
        GitHubRestClient(TOKEN, transport=transport).list_issue_matches()


def test_client_counts_previous_consecutive_failures_for_current_workflow() -> None:
    transport = FakeTransport(
        [
            FakeResponse(
                {
                    "head_branch": "main",
                    "id": 900,
                    "repository": {"full_name": REPOSITORY},
                    "run_number": 10,
                    "workflow_id": 42,
                }
            ),
            FakeResponse(
                {
                    "workflow_runs": [
                        {
                            "conclusion": "cancelled",
                            "id": 899,
                            "run_number": 9,
                            "status": "completed",
                        },
                        {
                            "conclusion": "failure",
                            "id": 898,
                            "run_number": 8,
                            "status": "completed",
                        },
                        {
                            "conclusion": "success",
                            "id": 897,
                            "run_number": 7,
                            "status": "completed",
                        },
                    ]
                }
            ),
        ]
    )

    count = GitHubRestClient(
        ACTIONS_TOKEN,
        transport=transport,
    ).previous_consecutive_failures(900, threshold=4)

    assert count == 2
    assert [request.method for request, _timeout in transport.calls] == ["GET", "GET"]
    assert [request.full_url for request, _timeout in transport.calls] == [
        "https://api.github.com/repos/H234598/desinfect/actions/runs/900",
        (
            "https://api.github.com/repos/H234598/desinfect/actions/workflows/42/runs"
            "?branch=main&status=completed&per_page=100&page=1"
        ),
    ]
    assert all(
        request.get_header("Authorization") == f"Bearer {ACTIONS_TOKEN}"
        for request, _timeout in transport.calls
    )


@pytest.mark.parametrize(
    "current",
    [
        {"head_branch": "feature", "id": 900, "repository": {"full_name": REPOSITORY}, "run_number": 10, "workflow_id": 42},
        {"head_branch": "main", "id": 901, "repository": {"full_name": REPOSITORY}, "run_number": 10, "workflow_id": 42},
        {"head_branch": "main", "id": 900, "repository": {"full_name": "attacker/repo"}, "run_number": 10, "workflow_id": 42},
    ],
)
def test_client_rejects_untrusted_current_run_identity(current: dict[str, object]) -> None:
    transport = FakeTransport([FakeResponse(current)])

    with pytest.raises(IncidentIssueError, match="Workflow-Lauf"):
        GitHubRestClient(
            ACTIONS_TOKEN,
            transport=transport,
        ).previous_consecutive_failures(900, threshold=2)

    assert len(transport.calls) == 1


def test_client_rejects_nonfinal_history_entry() -> None:
    transport = FakeTransport(
        [
            FakeResponse(
                {
                    "head_branch": "main",
                    "id": 900,
                    "repository": {"full_name": REPOSITORY},
                    "run_number": 10,
                    "workflow_id": 42,
                }
            ),
            FakeResponse(
                {
                    "workflow_runs": [
                        {
                            "conclusion": None,
                            "id": 899,
                            "run_number": 9,
                            "status": "in_progress",
                        }
                    ]
                }
            ),
        ]
    )

    with pytest.raises(IncidentIssueError, match="Workflow-Historie"):
        GitHubRestClient(
            ACTIONS_TOKEN,
            transport=transport,
        ).previous_consecutive_failures(900, threshold=2)


def test_client_rejects_nonstring_history_conclusion() -> None:
    transport = FakeTransport(
        [
            FakeResponse(
                {
                    "head_branch": "main",
                    "id": 900,
                    "repository": {"full_name": REPOSITORY},
                    "run_number": 10,
                    "workflow_id": 42,
                }
            ),
            FakeResponse(
                {
                    "workflow_runs": [
                        {
                            "conclusion": [],
                            "id": 899,
                            "run_number": 9,
                            "status": "completed",
                        }
                    ]
                }
            ),
        ]
    )

    with pytest.raises(IncidentIssueError, match="Workflow-Historie"):
        GitHubRestClient(
            ACTIONS_TOKEN,
            transport=transport,
        ).previous_consecutive_failures(900, threshold=2)


@pytest.mark.parametrize(
    ("matches", "status", "methods", "paths"),
    [
        (
            (),
            public_status(failures=2, state="degraded"),
            ["GET", "POST"],
            ["/labels/pipeline-incident", "/issues"],
        ),
        (
            (IssueMatch(number=7, state="open"),),
            public_status(failures=2, state="degraded"),
            ["PATCH"],
            ["/issues/7"],
        ),
        (
            (IssueMatch(number=7, state="closed"),),
            public_status(failures=2, state="degraded"),
            ["PATCH"],
            ["/issues/7"],
        ),
        (
            (IssueMatch(number=7, state="open"),),
            public_status(),
            ["POST", "PATCH"],
            ["/issues/7/comments", "/issues/7"],
        ),
    ],
)
def test_client_uses_exact_mutation_routes(
    matches: tuple[IssueMatch, ...],
    status: dict[str, object],
    methods: list[str],
    paths: list[str],
) -> None:
    plan = plan_incident_issue(status, matches)
    responses = [FakeResponse({}) for _ in methods]
    if plan.action == "create":
        responses[0] = FakeResponse({"name": LABEL})
    transport = FakeTransport(responses)

    GitHubRestClient(TOKEN, transport=transport).apply(
        plan,
        status=status,
        matches=matches,
        threshold=DEFAULT_THRESHOLD,
    )

    assert [request.method for request, _timeout in transport.calls] == methods
    assert [
        urlsplit(request.full_url).path.removeprefix("/repos/H234598/desinfect")
        for request, _timeout in transport.calls
    ] == paths
    payloads = [
        json.loads(request.data)
        for request, _timeout in transport.calls
        if request.data is not None
    ]
    if plan.action == "create":
        assert payloads == [
            {"body": plan.body, "labels": [LABEL], "title": TITLE_PREFIX}
        ]
    elif plan.action == "update":
        assert payloads == [
            {"body": plan.body, "labels": [LABEL], "title": TITLE_PREFIX}
        ]
    elif plan.action == "reopen":
        assert payloads == [
            {
                "body": plan.body,
                "labels": [LABEL],
                "state": "open",
                "title": TITLE_PREFIX,
            }
        ]
    else:
        assert payloads == [{"body": plan.comment}, {"state": "closed"}]


def test_client_creates_missing_label_before_issue() -> None:
    status = public_status(failures=2, state="degraded")
    plan = plan_incident_issue(status, ())
    transport = FakeTransport(
        [
            HTTPError(
                "https://api.github.com/repos/H234598/desinfect/labels/pipeline-incident",
                404,
                "Not Found",
                {},
                BytesIO(b"{}"),
            ),
            FakeResponse({"name": LABEL}),
            FakeResponse({"number": 17}),
        ]
    )

    GitHubRestClient(TOKEN, transport=transport).apply(
        plan,
        status=status,
        matches=(),
        threshold=DEFAULT_THRESHOLD,
    )

    assert [request.method for request, _timeout in transport.calls] == [
        "GET",
        "POST",
        "POST",
    ]
    assert [
        urlsplit(request.full_url).path.removeprefix("/repos/H234598/desinfect")
        for request, _timeout in transport.calls
    ] == ["/labels/pipeline-incident", "/labels", "/issues"]
    assert json.loads(transport.calls[1][0].data) == {
        "color": "B60205",
        "description": "Automatisch verwalteter RKI-Pipeline-Incident",
        "name": LABEL,
    }


def test_client_fails_closed_when_label_lookup_is_invalid() -> None:
    status = public_status(failures=2, state="degraded")
    plan = plan_incident_issue(status, ())
    transport = FakeTransport([FakeResponse({"name": "other"})])

    with pytest.raises(IncidentIssueError, match="Label"):
        GitHubRestClient(TOKEN, transport=transport).apply(
            plan,
            status=status,
            matches=(),
            threshold=DEFAULT_THRESHOLD,
        )

    assert len(transport.calls) == 1


def test_transport_failure_never_renders_token() -> None:
    transport = FakeTransport([RuntimeError(f"network failed for {TOKEN}")])

    with pytest.raises(IncidentIssueError) as caught:
        GitHubRestClient(TOKEN, transport=transport).list_issue_matches()

    assert TOKEN not in str(caught.value)


def test_plan_cli_is_offline_and_does_not_mutate_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "status.json"
    original = public_status(failures=2, state="degraded")
    path.write_text(stable_json_dumps(original), encoding="utf-8")

    def forbidden_transport(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("plan mode must stay offline")

    result = incident_main(
        ["--mode", "plan", "--status", str(path)],
        transport=forbidden_transport,
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["action"] == "create"
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_plan_cli_rejects_symlink_status_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target.json"
    link = tmp_path / "status.json"
    target.write_text(stable_json_dumps(public_status()), encoding="utf-8")
    link.symlink_to(target)

    result = incident_main(["--mode", "plan", "--status", str(link)])

    captured = capsys.readouterr()
    assert result == 1
    assert "reguläre Datei" in captured.err
    assert "Traceback" not in captured.err


def test_plan_cli_fails_closed_without_o_nofollow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "status.json"
    path.write_text(stable_json_dumps(public_status()), encoding="utf-8")
    monkeypatch.delattr("scripts.rki_pipeline.incident_issue.os.O_NOFOLLOW")

    result = incident_main(["--mode", "plan", "--status", str(path)])

    captured = capsys.readouterr()
    assert result == 1
    assert "O_NOFOLLOW" in captured.err
    assert "Traceback" not in captured.err


def test_plan_cli_rejects_directory_status_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "status"
    directory.mkdir()

    result = incident_main(["--mode", "plan", "--status", str(directory)])

    captured = capsys.readouterr()
    assert result == 1
    assert "reguläre Datei" in captured.err
    assert "Traceback" not in captured.err


def test_plan_cli_rejects_oversized_status_before_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "status.json"
    path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))

    def forbidden_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("oversized file must be rejected before os.read")

    monkeypatch.setattr("scripts.rki_pipeline.incident_issue.os.read", forbidden_read)
    result = incident_main(["--mode", "plan", "--status", str(path)])

    captured = capsys.readouterr()
    assert result == 1
    assert "Größenlimit" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"{\"value\":\xff}", "UTF-8"),
        (b'{"value":1,"value":2}', "doppelten Schlüssel"),
        (b'{"value":NaN}', "nichtendlichen Wert"),
        (b'{"value":1e9999}', "nichtendlichen Wert"),
    ],
)
def test_plan_cli_rejects_noncanonical_json_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    raw: bytes,
    message: str,
) -> None:
    path = tmp_path / "status.json"
    path.write_bytes(raw)

    result = incident_main(["--mode", "plan", "--status", str(path)])

    captured = capsys.readouterr()
    assert result == 1
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_plan_cli_projects_transaction_envelope_in_memory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_path = tmp_path / "status.json"
    manifest_path = tmp_path / "transaction-result.json"
    original = public_status(failures=1, state="degraded")
    status_path.write_text(stable_json_dumps(original), encoding="utf-8")
    manifest_path.write_text(
        stable_json_dumps({"schema_version": "1.0.0", "run_manifest": final_manifest("failed")}),
        encoding="utf-8",
    )

    result = incident_main(
        [
            "--mode",
            "plan",
            "--status",
            str(status_path),
            "--run-manifest",
            str(manifest_path),
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "create"
    assert r"manifest\_failed" in payload["body"]
    assert json.loads(status_path.read_text(encoding="utf-8")) == original


def test_plan_cli_rejects_nonfinal_manifest_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_path = tmp_path / "status.json"
    manifest_path = tmp_path / "run.json"
    status_path.write_text(stable_json_dumps(public_status()), encoding="utf-8")
    manifest_path.write_text(
        stable_json_dumps(
            new_run(
                workflow="rki-pipeline",
                trigger_source="schedule",
                run_mode="apply",
                storage_backend="lfs",
                run_id="incident-running",
                now="2026-08-04T12:01:00Z",
            )
        ),
        encoding="utf-8",
    )

    result = incident_main(
        [
            "--mode",
            "plan",
            "--status",
            str(status_path),
            "--run-manifest",
            str(manifest_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "projiziert" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("job_status", "synthetic_code"),
    [("failure", "ci_job_failure"), ("cancelled", "ci_job_cancelled")],
)
def test_job_failure_overrides_success_manifest_without_touching_status_clocks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    job_status: str,
    synthetic_code: str,
) -> None:
    status_path = tmp_path / "status.json"
    manifest_path = tmp_path / "run.json"
    original = public_status(failures=1, state="degraded")
    clocks = deepcopy(original["pipeline"])
    status_path.write_text(stable_json_dumps(original), encoding="utf-8")
    manifest_path.write_text(stable_json_dumps(final_manifest("success")), encoding="utf-8")

    result = incident_main(
        [
            "--mode",
            "plan",
            "--status",
            str(status_path),
            "--run-manifest",
            str(manifest_path),
            "--job-status",
            job_status,
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "create"
    assert synthetic_code.replace("_", r"\_") in payload["body"]
    persisted = json.loads(status_path.read_text(encoding="utf-8"))
    assert persisted["pipeline"] == clocks


def test_job_failure_without_manifest_counts_current_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status_path = tmp_path / "status.json"
    original = public_status(failures=1, state="degraded")
    status_path.write_text(stable_json_dumps(original), encoding="utf-8")

    result = incident_main(
        [
            "--mode",
            "plan",
            "--status",
            str(status_path),
            "--job-status",
            "failure",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "create"
    assert r"ci\_job\_failure" in payload["body"]
    assert json.loads(status_path.read_text(encoding="utf-8")) == original


def test_apply_cli_lists_then_creates_without_rendering_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(
        stable_json_dumps(public_status(failures=2, state="degraded")),
        encoding="utf-8",
    )
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    transport = FakeTransport(
        [
            FakeResponse([]),
            FakeResponse({"name": LABEL}),
            FakeResponse({"number": 17}),
        ]
    )

    result = incident_main(
        ["--mode", "apply", "--status", str(status_path)],
        transport=transport,
    )

    captured = capsys.readouterr()
    assert result == 0
    assert json.loads(captured.out)["action"] == "create"
    assert [request.method for request, _timeout in transport.calls] == [
        "GET",
        "GET",
        "POST",
    ]
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


def test_apply_cli_uses_actions_history_for_repeated_job_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(stable_json_dumps(public_status()), encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("ACTIONS_TOKEN", ACTIONS_TOKEN)
    monkeypatch.setenv("GITHUB_RUN_ID", "900")
    transport = FakeTransport(
        [
            FakeResponse(
                {
                    "head_branch": "main",
                    "id": 900,
                    "repository": {"full_name": REPOSITORY},
                    "run_number": 10,
                    "workflow_id": 42,
                }
            ),
            FakeResponse(
                {
                    "workflow_runs": [
                        {
                            "conclusion": "failure",
                            "id": 899,
                            "run_number": 9,
                            "status": "completed",
                        },
                        {
                            "conclusion": "success",
                            "id": 898,
                            "run_number": 8,
                            "status": "completed",
                        },
                    ]
                }
            ),
            FakeResponse([]),
            FakeResponse({"name": LABEL}),
            FakeResponse({"number": 17}),
        ]
    )

    result = incident_main(
        [
            "--mode",
            "apply",
            "--status",
            str(status_path),
            "--job-status",
            "failure",
            "--threshold",
            "2",
        ],
        transport=transport,
    )

    captured = capsys.readouterr()
    assert result == 0
    payload = json.loads(captured.out)
    assert payload["action"] == "create"
    assert "Aufeinanderfolgende Fehler: `2`" in payload["body"]
    assert ACTIONS_TOKEN not in captured.out
    assert ACTIONS_TOKEN not in captured.err


def test_apply_cli_job_failure_requires_actions_history_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(stable_json_dumps(public_status()), encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.delenv("ACTIONS_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_RUN_ID", "900")
    transport = FakeTransport([])

    result = incident_main(
        [
            "--mode",
            "apply",
            "--status",
            str(status_path),
            "--job-status",
            "failure",
        ],
        transport=transport,
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "ACTIONS_TOKEN" in captured.err
    assert transport.calls == []
