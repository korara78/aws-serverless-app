import json
import os
from decimal import Decimal

os.environ["ACCOUNTS_TABLE_NAME"] = "test-accounts"
os.environ["TRANSACTIONS_TABLE_NAME"] = "test-transactions"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import boto3
import pytest
from moto import mock_aws

FIXED_PRICE = Decimal("50000.00")


@pytest.fixture
def ledger_tables():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-accounts",
            AttributeDefinitions=[{"AttributeName": "account_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "account_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="test-transactions",
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
        yield


def _event(method, resource, path_params=None, body=None):
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params,
        "body": json.dumps(body) if body is not None else None,
    }


def _patch_price(monkeypatch, app, price=FIXED_PRICE):
    # Every unit test that touches BUY/SELL/price needs a deterministic,
    # network-free price — this pins app._fetch_coinbase_price (the one
    # function that actually makes an HTTP call) and resets the module-level
    # cache so no state leaks in from a previous test.
    app._price_cache["price"] = None
    app._price_cache["fetched_at"] = 0.0
    monkeypatch.setattr(app, "_fetch_coinbase_price", lambda: price)
    return price


def _create_account(app, usd_balance=None, btc_balance=None):
    body = {}
    if usd_balance is not None:
        body["usd_balance"] = str(usd_balance)
    if btc_balance is not None:
        body["btc_balance"] = str(btc_balance)
    response = app.lambda_handler(_event("POST", "/accounts", body=body), None)
    assert response["statusCode"] == 201
    return json.loads(response["body"])


def _buy_sell(app, account_id, trade_type, transaction_id, usd_amount):
    return app.lambda_handler(
        _event(
            "POST",
            "/accounts/{account_id}/transactions",
            path_params={"account_id": account_id},
            body={"transaction_id": transaction_id, "type": trade_type, "usd_amount": str(usd_amount)},
        ),
        None,
    )


# --- Account creation ------------------------------------------------------


def test_create_account_default_seed_is_realistic_and_nonzero(ledger_tables):
    import app

    account = _create_account(app)
    assert Decimal("25000") <= Decimal(account["usd_balance"]) <= Decimal("50000")
    assert Decimal("0.5") <= Decimal(account["btc_balance"]) <= Decimal("1.5")


def test_create_account_explicit_seed_writes_seed_row(ledger_tables):
    import app

    account = _create_account(app, usd_balance="1000", btc_balance="0")
    assert Decimal(account["usd_balance"]) == Decimal("1000")
    assert Decimal(account["btc_balance"]) == Decimal("0")

    history = json.loads(
        app.lambda_handler(
            _event(
                "GET",
                "/accounts/{account_id}/transactions",
                path_params={"account_id": account["account_id"]},
            ),
            None,
        )["body"]
    )
    assert len(history) == 1
    assert history[0]["type"] == "SEED"
    assert history[0]["status"] == "EXECUTED"


def test_get_account_not_found(ledger_tables):
    import app

    response = app.lambda_handler(
        _event("GET", "/accounts/{account_id}", path_params={"account_id": "missing"}), None
    )
    assert response["statusCode"] == 404


# --- Buy / sell happy path ---------------------------------------------------


def test_buy_reduces_usd_increases_btc(ledger_tables, monkeypatch):
    import app

    price = _patch_price(monkeypatch, app)
    account = _create_account(app, usd_balance="1000", btc_balance="0")

    response = _buy_sell(app, account["account_id"], "BUY", "txn-1", "500")
    assert response["statusCode"] == 201
    txn = json.loads(response["body"])
    assert Decimal(txn["btc_amount"]) == app._quantize_btc(Decimal("500") / price)

    updated = json.loads(
        app.lambda_handler(
            _event("GET", "/accounts/{account_id}", path_params={"account_id": account["account_id"]}),
            None,
        )["body"]
    )
    assert Decimal(updated["usd_balance"]) == Decimal("500")
    assert Decimal(updated["btc_balance"]) == app._quantize_btc(Decimal("500") / price)


def test_sell_reduces_btc_increases_usd(ledger_tables, monkeypatch):
    import app

    price = _patch_price(monkeypatch, app)
    account = _create_account(app, usd_balance="0", btc_balance="1")

    response = _buy_sell(app, account["account_id"], "SELL", "txn-1", "500")
    assert response["statusCode"] == 201

    updated = json.loads(
        app.lambda_handler(
            _event("GET", "/accounts/{account_id}", path_params={"account_id": account["account_id"]}),
            None,
        )["body"]
    )
    assert Decimal(updated["usd_balance"]) == Decimal("500")
    assert Decimal(updated["btc_balance"]) == Decimal("1") - app._quantize_btc(Decimal("500") / price)


# --- Insufficient funds -------------------------------------------------------


def test_buy_insufficient_funds_is_rejected_and_balance_unchanged(ledger_tables, monkeypatch):
    import app

    _patch_price(monkeypatch, app)
    account = _create_account(app, usd_balance="100", btc_balance="0")

    response = _buy_sell(app, account["account_id"], "BUY", "txn-1", "500")
    assert response["statusCode"] == 422
    assert json.loads(response["body"])["status"] == "REJECTED_INSUFFICIENT_FUNDS"

    updated = json.loads(
        app.lambda_handler(
            _event("GET", "/accounts/{account_id}", path_params={"account_id": account["account_id"]}),
            None,
        )["body"]
    )
    assert Decimal(updated["usd_balance"]) == Decimal("100")


def test_sell_insufficient_btc_is_rejected(ledger_tables, monkeypatch):
    import app

    _patch_price(monkeypatch, app)
    account = _create_account(app, usd_balance="0", btc_balance="0.001")

    response = _buy_sell(app, account["account_id"], "SELL", "txn-1", "500")
    assert response["statusCode"] == 422
    assert json.loads(response["body"])["status"] == "REJECTED_INSUFFICIENT_FUNDS"


# --- Idempotency ---------------------------------------------------------------


def test_duplicate_transaction_id_rejected_balance_changes_once(ledger_tables, monkeypatch):
    import app

    _patch_price(monkeypatch, app)
    account = _create_account(app, usd_balance="1000", btc_balance="0")

    first = _buy_sell(app, account["account_id"], "BUY", "txn-dup", "500")
    assert first["statusCode"] == 201

    second = _buy_sell(app, account["account_id"], "BUY", "txn-dup", "500")
    assert second["statusCode"] == 409
    assert json.loads(second["body"])["status"] == "REJECTED_DUPLICATE"

    updated = json.loads(
        app.lambda_handler(
            _event("GET", "/accounts/{account_id}", path_params={"account_id": account["account_id"]}),
            None,
        )["body"]
    )
    assert Decimal(updated["usd_balance"]) == Decimal("500")


def test_duplicate_transaction_id_after_rejection_is_also_rejected(ledger_tables, monkeypatch):
    import app

    _patch_price(monkeypatch, app)
    account = _create_account(app, usd_balance="10", btc_balance="0")

    first = _buy_sell(app, account["account_id"], "BUY", "txn-1", "500")
    assert first["statusCode"] == 422

    second = _buy_sell(app, account["account_id"], "BUY", "txn-1", "500")
    assert second["statusCode"] == 409
    assert json.loads(second["body"])["status"] == "REJECTED_DUPLICATE"


# --- Reconciliation --------------------------------------------------------


def test_verify_balance_matches_for_brand_new_account(ledger_tables):
    import app

    account = _create_account(app, usd_balance="1000", btc_balance="1")
    result = json.loads(
        app.lambda_handler(
            _event(
                "GET",
                "/accounts/{account_id}/balance/verify",
                path_params={"account_id": account["account_id"]},
            ),
            None,
        )["body"]
    )
    assert result["matches"] is True
    assert Decimal(result["computed_usd_balance"]) == Decimal("1000")
    assert Decimal(result["computed_btc_balance"]) == Decimal("1")


def test_verify_balance_matches_after_several_transactions(ledger_tables, monkeypatch):
    import app

    _patch_price(monkeypatch, app)
    account = _create_account(app, usd_balance="1000", btc_balance="0")
    _buy_sell(app, account["account_id"], "BUY", "txn-1", "400")
    _buy_sell(app, account["account_id"], "SELL", "txn-2", "100")

    result = json.loads(
        app.lambda_handler(
            _event(
                "GET",
                "/accounts/{account_id}/balance/verify",
                path_params={"account_id": account["account_id"]},
            ),
            None,
        )["body"]
    )
    assert result["matches"] is True


def test_verify_balance_detects_injected_mismatch(ledger_tables):
    import app

    account = _create_account(app, usd_balance="1000", btc_balance="1")

    # Simulate a cached-balance bug independent of app logic: mutate the
    # Accounts row directly, bypassing the ledger entirely.
    app.accounts_table.update_item(
        Key={"account_id": account["account_id"]},
        UpdateExpression="SET usd_balance = :bad",
        ExpressionAttributeValues={":bad": Decimal("999999")},
    )

    result = json.loads(
        app.lambda_handler(
            _event(
                "GET",
                "/accounts/{account_id}/balance/verify",
                path_params={"account_id": account["account_id"]},
            ),
            None,
        )["body"]
    )
    assert result["matches"] is False


# --- Transaction history -----------------------------------------------------


def test_list_transactions_includes_seed_and_is_chronological(ledger_tables, monkeypatch):
    import app

    _patch_price(monkeypatch, app)
    account = _create_account(app, usd_balance="1000", btc_balance="0")
    _buy_sell(app, account["account_id"], "BUY", "txn-1", "100")
    _buy_sell(app, account["account_id"], "BUY", "txn-2", "100")

    history = json.loads(
        app.lambda_handler(
            _event(
                "GET",
                "/accounts/{account_id}/transactions",
                path_params={"account_id": account["account_id"]},
            ),
            None,
        )["body"]
    )
    assert [t["type"] for t in history] == ["SEED", "BUY", "BUY"]
    assert history == sorted(history, key=lambda t: t["executed_at"])


# --- Append-only enforcement ---------------------------------------------------


def test_no_update_or_delete_route_exists_for_transactions(ledger_tables):
    import app

    for method in ("PUT", "DELETE"):
        response = app.lambda_handler(
            _event(method, "/accounts/{account_id}/transactions", path_params={"account_id": "x"}),
            None,
        )
        assert response["statusCode"] == 400


# --- Boundary math ------------------------------------------------------------


def test_boundary_exact_balance_buy_leaves_exactly_zero(ledger_tables, monkeypatch):
    import app

    _patch_price(monkeypatch, app)
    account = _create_account(app, usd_balance="500", btc_balance="0")

    response = _buy_sell(app, account["account_id"], "BUY", "txn-1", "500")
    assert response["statusCode"] == 201

    updated = json.loads(
        app.lambda_handler(
            _event("GET", "/accounts/{account_id}", path_params={"account_id": account["account_id"]}),
            None,
        )["body"]
    )
    assert Decimal(updated["usd_balance"]) == Decimal("0")


def test_fractional_btc_amount_precision_reconciles(ledger_tables, monkeypatch):
    import app

    _patch_price(monkeypatch, app, price=Decimal("3"))
    account = _create_account(app, usd_balance="100", btc_balance="0")

    response = _buy_sell(app, account["account_id"], "BUY", "txn-1", "100")
    assert response["statusCode"] == 201

    result = json.loads(
        app.lambda_handler(
            _event(
                "GET",
                "/accounts/{account_id}/balance/verify",
                path_params={"account_id": account["account_id"]},
            ),
            None,
        )["body"]
    )
    assert result["matches"] is True


# A regression test for the high-precision SEED+BUY reconciliation bug
# (see Guide 6) deliberately does NOT live here: moto's UpdateExpression
# arithmetic goes through Python's own Decimal machinery under the same
# process-wide context as the test itself, so both "cached" and "computed"
# would round identically and the test would pass whether or not the real
# fix (decimal.getcontext().prec = 38) is even in place — a false sense of
# coverage. Confirmed against real DynamoDB Local that it reproduces the
# same discrepancy AWS does; the real regression test lives in
# tests/integration/test_api_integration.py instead.


# --- Coinbase price feed -------------------------------------------------------


def test_price_endpoint_returns_parsed_price(ledger_tables, monkeypatch):
    import app

    price = _patch_price(monkeypatch, app)
    response = app.lambda_handler(_event("GET", "/price"), None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert Decimal(body["btc_usd"]) == price
    assert body["stale"] is False


def test_price_falls_back_to_stale_cache_on_fetch_failure(ledger_tables, monkeypatch):
    import app
    import urllib.error

    price = _patch_price(monkeypatch, app)
    app.lambda_handler(_event("GET", "/price"), None)  # warms the cache

    def _boom():
        raise urllib.error.URLError("Coinbase is down")

    monkeypatch.setattr(app, "_fetch_coinbase_price", _boom)
    app._price_cache["fetched_at"] = 0.0  # force the TTL to be treated as expired

    response = app.lambda_handler(_event("GET", "/price"), None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert Decimal(body["btc_usd"]) == price
    assert body["stale"] is True


def test_price_unavailable_with_no_cache_and_failed_fetch(ledger_tables, monkeypatch):
    import app
    import urllib.error

    app._price_cache["price"] = None
    app._price_cache["fetched_at"] = 0.0

    def _boom():
        raise urllib.error.URLError("Coinbase is down")

    monkeypatch.setattr(app, "_fetch_coinbase_price", _boom)

    response = app.lambda_handler(_event("GET", "/price"), None)
    assert response["statusCode"] == 503


def test_coinbase_rates_usd_string_is_parsed_as_decimal(ledger_tables, monkeypatch):
    # Regression test for the exact bug caught during endpoint verification:
    # Coinbase returns rates.USD as a string ("62876.95"), not a number.
    import app

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake_payload = {"data": {"currency": "BTC", "rates": {"USD": "62876.95"}}}
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=5: _FakeResponse(fake_payload)
    )

    price = app._fetch_coinbase_price()
    assert isinstance(price, Decimal)
    assert price == Decimal("62876.95")
