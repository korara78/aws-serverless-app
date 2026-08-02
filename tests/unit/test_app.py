import json
import os

os.environ["TABLE_NAME"] = "test-items"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def dynamodb_table():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-items",
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _event(method, path_params=None, body=None):
    return {
        "httpMethod": method,
        "pathParameters": path_params,
        "body": json.dumps(body) if body is not None else None,
    }


def test_create_item_requires_name(dynamodb_table):
    import app

    response = app.lambda_handler(_event("POST", body={}), None)
    assert response["statusCode"] == 400


def test_create_and_get_item(dynamodb_table):
    import app

    create_response = app.lambda_handler(_event("POST", body={"name": "Widget"}), None)
    assert create_response["statusCode"] == 201
    created = json.loads(create_response["body"])

    get_response = app.lambda_handler(_event("GET", path_params={"id": created["id"]}), None)
    assert get_response["statusCode"] == 200
    assert json.loads(get_response["body"])["name"] == "Widget"


def test_get_item_not_found(dynamodb_table):
    import app

    response = app.lambda_handler(_event("GET", path_params={"id": "missing"}), None)
    assert response["statusCode"] == 404


def test_list_items(dynamodb_table):
    import app

    app.lambda_handler(_event("POST", body={"name": "A"}), None)
    app.lambda_handler(_event("POST", body={"name": "B"}), None)
    response = app.lambda_handler(_event("GET"), None)
    assert response["statusCode"] == 200
    assert len(json.loads(response["body"])) == 2


def test_update_item(dynamodb_table):
    import app

    created = json.loads(app.lambda_handler(_event("POST", body={"name": "A"}), None)["body"])
    response = app.lambda_handler(
        _event("PUT", path_params={"id": created["id"]}, body={"name": "A2"}), None
    )
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["name"] == "A2"


def test_update_item_not_found(dynamodb_table):
    import app

    response = app.lambda_handler(
        _event("PUT", path_params={"id": "missing"}, body={"name": "A2"}), None
    )
    assert response["statusCode"] == 404


def test_delete_item(dynamodb_table):
    import app

    created = json.loads(app.lambda_handler(_event("POST", body={"name": "A"}), None)["body"])
    response = app.lambda_handler(_event("DELETE", path_params={"id": created["id"]}), None)
    assert response["statusCode"] == 204

    follow_up = app.lambda_handler(_event("GET", path_params={"id": created["id"]}), None)
    assert follow_up["statusCode"] == 404


def test_unsupported_route(dynamodb_table):
    import app

    response = app.lambda_handler(_event("PATCH"), None)
    assert response["statusCode"] == 400
