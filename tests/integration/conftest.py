import subprocess
import time
import uuid

import boto3
import pytest

DYNAMODB_PORT = 8000
ACCOUNTS_TABLE_NAME = f"integration-accounts-{uuid.uuid4().hex[:8]}"
TRANSACTIONS_TABLE_NAME = f"integration-transactions-{uuid.uuid4().hex[:8]}"


def _client(endpoint_url):
    return boto3.client(
        "dynamodb",
        endpoint_url=endpoint_url,
        region_name="us-east-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )


def _wait_for_dynamodb(endpoint_url, timeout=30):
    client = _client(endpoint_url)
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            client.list_tables()
            return
        except Exception as exc:  # noqa: BLE001 - retry until timeout, then raise below
            last_error = exc
            time.sleep(1)
    raise RuntimeError("DynamoDB Local did not become ready in time") from last_error


@pytest.fixture(scope="session")
def dynamodb_local():
    container_name = f"dynamodb-local-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", container_name,
            "-p", f"{DYNAMODB_PORT}:8000", "amazon/dynamodb-local:3.3.1",
            # -sharedDb: without it, DynamoDB Local partitions data by the
            # AWS access key used in each request — this fixture is
            # internally consistent (always "local"/"local") so it isn't
            # currently affected, but see the Makefile's test-local-invoke
            # target for the real bug this caused there. Cheap insurance.
            "-jar", "DynamoDBLocal.jar", "-inMemory", "-sharedDb",
        ],
        check=True,
        capture_output=True,
    )
    endpoint_url = f"http://localhost:{DYNAMODB_PORT}"
    try:
        _wait_for_dynamodb(endpoint_url)
        yield endpoint_url
    finally:
        subprocess.run(["docker", "stop", container_name], check=False, capture_output=True)


@pytest.fixture
def ledger_tables(dynamodb_local, monkeypatch):
    monkeypatch.setenv("DYNAMODB_ENDPOINT_URL", dynamodb_local)
    monkeypatch.setenv("ACCOUNTS_TABLE_NAME", ACCOUNTS_TABLE_NAME)
    monkeypatch.setenv("TRANSACTIONS_TABLE_NAME", TRANSACTIONS_TABLE_NAME)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "local")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "local")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    client = _client(dynamodb_local)
    client.create_table(
        TableName=ACCOUNTS_TABLE_NAME,
        AttributeDefinitions=[{"AttributeName": "account_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "account_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName=TRANSACTIONS_TABLE_NAME,
        AttributeDefinitions=[
            {"AttributeName": "transaction_id", "AttributeType": "S"},
            {"AttributeName": "account_id", "AttributeType": "S"},
            {"AttributeName": "executed_at", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "transaction_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "AccountIndex",
                "KeySchema": [
                    {"AttributeName": "account_id", "KeyType": "HASH"},
                    {"AttributeName": "executed_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    try:
        yield
    finally:
        client.delete_table(TableName=ACCOUNTS_TABLE_NAME)
        client.delete_table(TableName=TRANSACTIONS_TABLE_NAME)
