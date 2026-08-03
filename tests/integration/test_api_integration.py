import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal


def _reload_app():
    import app

    importlib.reload(app)
    return app


def _event(method, resource, path_params=None, body=None):
    return {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params,
        "body": json.dumps(body) if body is not None else None,
    }


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


def test_full_lifecycle_against_real_dynamodb_and_coinbase(ledger_tables):
    # The one integration test that hits the real Coinbase endpoint, not a
    # mocked price — proves the whole real chain (DynamoDB Local + a real
    # external HTTP call) works end to end. Every other test here fixes the
    # price so its math is deterministic; this one deliberately doesn't.
    app = _reload_app()

    account = _create_account(app, usd_balance="1000", btc_balance="0")

    buy_response = _buy_sell(app, account["account_id"], "BUY", "int-txn-1", "400")
    assert buy_response["statusCode"] == 201
    txn = json.loads(buy_response["body"])
    price_used = Decimal(txn["btc_price_at_execution"])
    assert Decimal(txn["btc_amount"]) == Decimal("400") / price_used

    verify = json.loads(
        app.lambda_handler(
            _event(
                "GET",
                "/accounts/{account_id}/balance/verify",
                path_params={"account_id": account["account_id"]},
            ),
            None,
        )["body"]
    )
    assert verify["matches"] is True

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
    assert [t["type"] for t in history] == ["SEED", "BUY"]


def test_duplicate_transaction_id_against_real_dynamodb(ledger_tables, monkeypatch):
    app = _reload_app()
    app._price_cache["price"] = None
    app._price_cache["fetched_at"] = 0.0
    monkeypatch.setattr(app, "_fetch_coinbase_price", lambda: Decimal("50000.00"))

    account = _create_account(app, usd_balance="1000", btc_balance="0")

    first = _buy_sell(app, account["account_id"], "BUY", "dup-txn", "500")
    assert first["statusCode"] == 201

    second = _buy_sell(app, account["account_id"], "BUY", "dup-txn", "500")
    assert second["statusCode"] == 409
    assert json.loads(second["body"])["status"] == "REJECTED_DUPLICATE"

    updated = json.loads(
        app.lambda_handler(
            _event("GET", "/accounts/{account_id}", path_params={"account_id": account["account_id"]}),
            None,
        )["body"]
    )
    assert Decimal(updated["usd_balance"]) == Decimal("500")


def test_concurrent_buy_requests_that_together_exceed_balance(ledger_tables, monkeypatch):
    # The core "no negative balances under concurrency" proof (rule 2) —
    # moto is single-threaded/in-process, so it can't actually exercise a
    # real race on the same item. Real DynamoDB Local, hit from two threads
    # at once, can. Two BUY requests for 600 each against a 700 balance:
    # only one can fit, and the account must never go negative either way.
    app = _reload_app()
    app._price_cache["price"] = None
    app._price_cache["fetched_at"] = 0.0
    monkeypatch.setattr(app, "_fetch_coinbase_price", lambda: Decimal("1"))  # price=1 keeps usd_amount == btc_amount

    account = _create_account(app, usd_balance="700", btc_balance="0")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_buy_sell, app, account["account_id"], "BUY", "race-txn-a", "600"),
            pool.submit(_buy_sell, app, account["account_id"], "BUY", "race-txn-b", "600"),
        ]
        results = [f.result() for f in futures]

    status_codes = sorted(r["statusCode"] for r in results)
    assert status_codes == [201, 422]

    updated = json.loads(
        app.lambda_handler(
            _event("GET", "/accounts/{account_id}", path_params={"account_id": account["account_id"]}),
            None,
        )["body"]
    )
    assert Decimal(updated["usd_balance"]) == Decimal("100")
    assert Decimal(updated["usd_balance"]) >= 0

    verify = json.loads(
        app.lambda_handler(
            _event(
                "GET",
                "/accounts/{account_id}/balance/verify",
                path_params={"account_id": account["account_id"]},
            ),
            None,
        )["body"]
    )
    assert verify["matches"] is True
