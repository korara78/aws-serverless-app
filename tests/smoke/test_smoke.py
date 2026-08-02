import os
import uuid

import requests

BASE_URL = os.environ["API_BASE_URL"].rstrip("/")


def test_full_crud_lifecycle_against_live_endpoint():
    create_response = requests.post(
        f"{BASE_URL}/items", json={"name": f"smoke-{uuid.uuid4().hex[:8]}"}
    )
    assert create_response.status_code == 201
    item = create_response.json()

    get_response = requests.get(f"{BASE_URL}/items/{item['id']}")
    assert get_response.status_code == 200
    assert get_response.json()["name"] == item["name"]

    delete_response = requests.delete(f"{BASE_URL}/items/{item['id']}")
    assert delete_response.status_code == 204

    follow_up = requests.get(f"{BASE_URL}/items/{item['id']}")
    assert follow_up.status_code == 404
