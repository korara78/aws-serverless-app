import json
import os
import uuid

import boto3

TABLE_NAME = os.environ.get("TABLE_NAME", "ItemsTable")

_dynamodb_kwargs = {}
_endpoint_url = os.environ.get("DYNAMODB_ENDPOINT_URL")
if _endpoint_url:
    _dynamodb_kwargs["endpoint_url"] = _endpoint_url

dynamodb = boto3.resource("dynamodb", **_dynamodb_kwargs)
table = dynamodb.Table(TABLE_NAME)


def _response(status_code, body=None):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body) if body is not None else "",
    }


def lambda_handler(event, context):
    method = event.get("httpMethod")
    path_params = event.get("pathParameters") or {}
    item_id = path_params.get("id")

    try:
        if method == "POST":
            return create_item(event)
        if method == "GET" and item_id:
            return get_item(item_id)
        if method == "GET":
            return list_items()
        if method == "PUT" and item_id:
            return update_item(item_id, event)
        if method == "DELETE" and item_id:
            return delete_item(item_id)
        return _response(400, {"message": "Unsupported route"})
    except ValueError as exc:
        return _response(400, {"message": str(exc)})


def create_item(event):
    body = json.loads(event.get("body") or "{}")
    if "name" not in body:
        raise ValueError("'name' is required")
    item = {**body, "id": str(uuid.uuid4()), "name": body["name"]}
    table.put_item(Item=item)
    return _response(201, item)


def get_item(item_id):
    item = table.get_item(Key={"id": item_id}).get("Item")
    if not item:
        return _response(404, {"message": "Item not found"})
    return _response(200, item)


def list_items():
    items = table.scan().get("Items", [])
    return _response(200, items)


def update_item(item_id, event):
    existing = table.get_item(Key={"id": item_id}).get("Item")
    if not existing:
        return _response(404, {"message": "Item not found"})
    body = json.loads(event.get("body") or "{}")
    updated = {**existing, **body, "id": item_id}
    table.put_item(Item=updated)
    return _response(200, updated)


def delete_item(item_id):
    existing = table.get_item(Key={"id": item_id}).get("Item")
    if not existing:
        return _response(404, {"message": "Item not found"})
    table.delete_item(Key={"id": item_id})
    return _response(204)
