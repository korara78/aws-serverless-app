import importlib
import json


def _reload_app():
    import app

    importlib.reload(app)
    return app


def _event(method, path_params=None, body=None):
    return {
        "httpMethod": method,
        "pathParameters": path_params,
        "body": json.dumps(body) if body is not None else None,
    }


def test_full_crud_lifecycle_against_real_dynamodb(items_table):
    app = _reload_app()

    create_response = app.lambda_handler(_event("POST", body={"name": "Integration Widget"}), None)
    assert create_response["statusCode"] == 201
    created = json.loads(create_response["body"])

    list_response = app.lambda_handler(_event("GET"), None)
    assert any(item["id"] == created["id"] for item in json.loads(list_response["body"]))

    update_response = app.lambda_handler(
        _event("PUT", path_params={"id": created["id"]}, body={"name": "Updated"}), None
    )
    assert json.loads(update_response["body"])["name"] == "Updated"

    delete_response = app.lambda_handler(_event("DELETE", path_params={"id": created["id"]}), None)
    assert delete_response["statusCode"] == 204

    get_response = app.lambda_handler(_event("GET", path_params={"id": created["id"]}), None)
    assert get_response["statusCode"] == 404
