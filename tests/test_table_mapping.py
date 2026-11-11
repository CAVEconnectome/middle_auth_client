"""Tests for table->dataset mapping failures.

Why this exists: when a datastack/aligned volume/PCG table was added to a service but its
middle_auth service table mapping was not, lookups raised a bare RuntimeError. List views that
check every row then failed for every user, and decorated endpoints answered with an unexplained
400. These tests pin the typed errors, the never-raising permission helpers, the per-request
problem log, and the on_unmapped option of auth_requires_permission.

The auth server is replaced by a fake `session.get`; no network is used.
"""

import flask
import pytest
import requests

import middle_auth_client
from middle_auth_client import decorators

# (namespace, table) -> dataset. Missing -> 404. Values that are exceptions are raised.
MAPPINGS = {}


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body
        self.content = b"" if body is None else repr(body).encode()

    def json(self):
        return self._body


def fake_get(url, headers=None, timeout=None):
    _, _, path = url.partition("/api/v1/service/")
    namespace, _, rest = path.partition("/table/")
    table = rest[: -len("/dataset")]
    value = MAPPINGS.get((namespace, table))
    if isinstance(value, Exception):
        raise value
    if isinstance(value, FakeResponse):
        return value
    if value is None:
        return FakeResponse(404)
    return FakeResponse(200, value)


USERS = {
    "user-token": {
        "id": 1,
        "admin": False,
        "permissions_v2": {"minnie": ["view"]},
        "groups": [],
    },
    "admin-token": {
        "id": 2,
        "admin": True,
        "permissions_v2": {"minnie": ["view"]},
        "groups": [],
    },
}


@pytest.fixture(autouse=True)
def fake_auth(monkeypatch):
    MAPPINGS.clear()
    MAPPINGS.update(
        {
            ("datastack", "good"): "minnie",
            ("datastack", "other"): "secret",
        }
    )
    monkeypatch.setattr(decorators.session, "get", fake_get)
    monkeypatch.setattr(decorators, "get_user_cache", lambda token: USERS.get(token))
    monkeypatch.setattr(decorators, "clear_user_cache_maybe", lambda token: None)
    decorators.dataset_from_table_id.cache_clear()


@pytest.fixture
def app():
    app = flask.Flask(__name__)
    app.config["AUTH_TOKEN"] = "service-token"

    @app.route("/strict/<table_id>")
    @middle_auth_client.auth_requires_permission("view", resource_namespace="datastack")
    def strict(table_id):
        return flask.jsonify({"table": table_id, "problems": middle_auth_client.auth_problems()})

    @app.route("/lenient/<table_id>")
    @middle_auth_client.auth_requires_permission(
        "view", resource_namespace="datastack", on_unmapped="admin_passthrough"
    )
    def lenient(table_id):
        return flask.jsonify({"table": table_id, "problems": middle_auth_client.auth_problems()})

    @app.route("/list")
    @middle_auth_client.auth_required
    def listing():
        tables = ["good", "other", "new_unmapped"]
        return flask.jsonify(
            {
                "visible": middle_auth_client.filter_permitted(tables, "view", "datastack"),
                "problems": middle_auth_client.auth_problems(),
            }
        )

    return app


def get(app, path, token="user-token"):
    return app.test_client().get(path, headers={"Authorization": f"Bearer {token}"})


# ------------------------------------------------------------------- typed errors


def test_unmapped_table_raises_not_found():
    with pytest.raises(middle_auth_client.TableMappingNotFound) as e:
        decorators.dataset_from_table_id("datastack", "new_unmapped", "tok")
    assert e.value.kind == "unmapped"
    assert "(datastack, new_unmapped)" in e.value.remediation


def test_typed_errors_are_still_runtime_errors():
    # Existing services catch RuntimeError; they must keep working.
    with pytest.raises(RuntimeError, match="failed to lookup dataset"):
        decorators.dataset_from_table_id("datastack", "new_unmapped", "tok")


def test_network_error_is_auth_unavailable():
    MAPPINGS[("datastack", "good")] = requests.ConnectionError("refused")
    with pytest.raises(middle_auth_client.AuthServiceUnavailable) as e:
        decorators.dataset_from_table_id("datastack", "good", "tok")
    assert e.value.kind == "auth_unavailable"


def test_server_error_is_auth_unavailable():
    MAPPINGS[("datastack", "good")] = FakeResponse(502)
    with pytest.raises(middle_auth_client.AuthServiceUnavailable):
        decorators.dataset_from_table_id("datastack", "good", "tok")


def test_other_status_is_generic_mapping_error():
    MAPPINGS[("datastack", "good")] = FakeResponse(401)
    with pytest.raises(middle_auth_client.TableMappingError) as e:
        decorators.dataset_from_table_id("datastack", "good", "tok")
    assert type(e.value) is middle_auth_client.TableMappingError
    assert e.value.status_code == 401


# ------------------------------------------------------------------- never-raising helpers


def test_filter_permitted_drops_unmapped_and_records_it(app):
    resp = get(app, "/list")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["visible"] == ["good"]
    (problem,) = body["problems"]
    assert problem["table_id"] == "new_unmapped" and problem["kind"] == "unmapped"


def test_filter_permitted_survives_auth_outage(app):
    MAPPINGS[("datastack", "good")] = requests.Timeout("slow")
    body = get(app, "/list").get_json()
    assert body["visible"] == []
    assert {p["kind"] for p in body["problems"]} == {"auth_unavailable", "unmapped"}


def test_problems_are_deduplicated(app):
    with app.test_request_context():
        flask.g.auth_user = USERS["user-token"]
        for _ in range(3):
            middle_auth_client.user_has_permission("view", "new_unmapped", "datastack")
        assert len(middle_auth_client.auth_problems()) == 1


# ------------------------------------------------------------------- decorator


def test_strict_mapped_table_still_checks_permission(app):
    assert get(app, "/strict/good").status_code == 200
    resp = get(app, "/strict/other")
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "missing_permission"


def test_strict_unmapped_is_explained_but_keeps_its_code(app):
    resp = get(app, "/strict/new_unmapped")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "invalid_table_id"  # unchanged for existing clients
    assert "not mapped" in body["message"]
    assert body["data"]["kind"] == "unmapped"
    assert "(datastack, new_unmapped)" in body["data"]["remediation"]


def test_strict_auth_outage_is_503(app):
    MAPPINGS[("datastack", "good")] = requests.ConnectionError("refused")
    resp = get(app, "/strict/good")
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "auth_service_unavailable"


def test_passthrough_admin_reaches_view_with_problem(app):
    resp = get(app, "/lenient/new_unmapped", token="admin-token")
    assert resp.status_code == 200
    assert resp.get_json()["problems"][0]["table_id"] == "new_unmapped"


def test_passthrough_user_gets_plain_403(app):
    resp = get(app, "/lenient/new_unmapped")
    assert resp.status_code == 403
    assert "new_unmapped" not in resp.get_data(as_text=True)


def test_passthrough_does_not_bypass_auth_outage(app):
    MAPPINGS[("datastack", "new_unmapped")] = requests.ConnectionError("refused")
    resp = get(app, "/lenient/new_unmapped", token="admin-token")
    assert resp.status_code == 503


def test_passthrough_mapped_table_is_unchanged(app):
    assert get(app, "/lenient/good").status_code == 200
    assert get(app, "/lenient/other").status_code == 403


def test_invalid_on_unmapped_value():
    with pytest.raises(ValueError):
        middle_auth_client.auth_requires_permission("view", on_unmapped="ignore")


def test_is_global_admin(app):
    with app.test_request_context():
        assert middle_auth_client.is_global_admin() is False
        flask.g.auth_user = USERS["admin-token"]
        assert middle_auth_client.is_global_admin() is True
