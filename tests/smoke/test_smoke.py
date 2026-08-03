import os
import uuid
from decimal import Decimal

import requests

BASE_URL = os.environ["API_BASE_URL"].rstrip("/")

# No delete-account endpoint exists (deliberate — real ledgers don't delete
# accounts, and Transactions rows are append-only by design). Accounts
# created here accumulate in the live deployed environment, same as every
# other suite in this portfolio that creates real, un-cleaned-up records
# against a live target.


def test_full_ledger_lifecycle_against_live_endpoint():
    price_response = requests.get(f"{BASE_URL}/price")
    assert price_response.status_code == 200
    assert "btc_usd" in price_response.json()

    create_response = requests.post(
        f"{BASE_URL}/accounts",
        json={"display_name": f"smoke-{uuid.uuid4().hex[:8]}", "usd_balance": "1000", "btc_balance": "0"},
    )
    assert create_response.status_code == 201
    account = create_response.json()
    assert Decimal(account["usd_balance"]) == Decimal("1000")

    transaction_id = str(uuid.uuid4())
    buy_response = requests.post(
        f"{BASE_URL}/accounts/{account['account_id']}/transactions",
        json={"transaction_id": transaction_id, "type": "BUY", "usd_amount": "400"},
    )
    assert buy_response.status_code == 201
    txn = buy_response.json()
    assert txn["status"] == "EXECUTED"

    duplicate_response = requests.post(
        f"{BASE_URL}/accounts/{account['account_id']}/transactions",
        json={"transaction_id": transaction_id, "type": "BUY", "usd_amount": "400"},
    )
    assert duplicate_response.status_code == 409
    assert duplicate_response.json()["status"] == "REJECTED_DUPLICATE"

    verify_response = requests.get(f"{BASE_URL}/accounts/{account['account_id']}/balance/verify")
    assert verify_response.status_code == 200
    assert verify_response.json()["matches"] is True

    history_response = requests.get(f"{BASE_URL}/accounts/{account['account_id']}/transactions")
    assert history_response.status_code == 200
    assert [t["type"] for t in history_response.json()] == ["SEED", "BUY"]
