import decimal
import json
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer

# boto3's own Decimal -> DynamoDB Number serialization uses plain str(),
# which switches to scientific notation past a threshold (Decimal('0E-8'),
# Decimal('1E-7'), etc — not just for zero, any sufficiently small/precise
# value). DynamoDB's Number type flatly rejects scientific notation
# (confirmed live: `ValueError: invalid literal for int() with base 10:
# '0E-8'` surfaced from inside a TransactWriteItems call). Patched here,
# once, at the class level, so every write path — this module's own
# low-level transact_write_items calls *and* the resource-level Table API's
# put_item/update_item, which uses the exact same TypeSerializer internally
# — always emits plain fixed-point, not just the paths this module wrote by
# hand.
if not getattr(TypeSerializer.serialize, "_patched_for_decimal", False):
    _original_serialize = TypeSerializer.serialize

    def _patched_serialize(self, value):
        if isinstance(value, Decimal):
            return {"N": format(value, "f")}
        return _original_serialize(self, value)

    _patched_serialize._patched_for_decimal = True
    TypeSerializer.serialize = _patched_serialize

ACCOUNTS_TABLE_NAME = os.environ.get("ACCOUNTS_TABLE_NAME", "Accounts")
TRANSACTIONS_TABLE_NAME = os.environ.get("TRANSACTIONS_TABLE_NAME", "Transactions")
COINBASE_URL = "https://api.coinbase.com/v2/exchange-rates?currency=BTC"
PRICE_CACHE_TTL_SECONDS = 45

# Standard Bitcoin precision — a satoshi, 1e-8 BTC. Every BTC-denominated
# value (a SEED balance, a computed btc_amount) is quantized to this before
# it's used or stored anywhere, not left as the raw, arbitrary-length result
# of a division. This isn't just cosmetic: confirmed live, an unquantized
# btc_amount (up to 28+ significant digits from a single division) summed
# with a low-precision SEED balance can need more digits to represent
# exactly than Python's default Decimal context (28) preserves — a false
# reconciliation mismatch on genuinely correct data. Widening Python's
# context to DynamoDB's own 38-digit ceiling "fixes" that but only trades
# it for a worse failure: DynamoDB itself rejects TransactWriteItems
# whose arithmetic would need *more* than 38 digits to represent exactly
# (`ValidationException: DynamoDB only supports precision up to 38 digits`).
# Quantizing at the source — the only point values actually need bounding —
# avoids both, and is also just correct: real BTC amounts don't have
# unlimited precision, they're granular to the satoshi.
SATOSHI = Decimal("0.00000001")


def _quantize_btc(amount):
    return amount.quantize(SATOSHI, rounding=decimal.ROUND_DOWN)


_dynamodb_kwargs = {}
_endpoint_url = os.environ.get("DYNAMODB_ENDPOINT_URL")
if _endpoint_url:
    _dynamodb_kwargs["endpoint_url"] = _endpoint_url

dynamodb = boto3.resource("dynamodb", **_dynamodb_kwargs)
accounts_table = dynamodb.Table(ACCOUNTS_TABLE_NAME)
transactions_table = dynamodb.Table(TRANSACTIONS_TABLE_NAME)

# A resource's `.meta.client` carries DynamoDB-specific event hooks meant for
# the high-level Table API's native-Python-type item transformation. Reusing
# it for transact_write_items — which takes already-low-level-formatted
# AttributeValue items — makes those hooks try to re-transform items that are
# already in the wire format, corrupting them. A plain client, created
# independently, doesn't have those hooks attached and is the documented way
# to make low-level calls (confirmed against a real TransactionCanceledException
# with a "cannot use DynamoType as a dict key" error before this split existed).
dynamodb_client = boto3.client("dynamodb", **_dynamodb_kwargs)

_serializer = TypeSerializer()
_price_cache = {"price": None, "fetched_at": 0.0}


class PriceUnavailableError(Exception):
    pass


def _to_dynamo(item):
    """Low-level AttributeValue map, for transact_write_items — the resource-level
    Table API accepts native Python types directly, but the low-level client
    used for transactions does not."""
    return {k: _serializer.serialize(v) for k, v in item.items()}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _response(status_code, body=None):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        # default=str renders Decimal as its exact decimal string, not a
        # float — avoids silently reintroducing float rounding at the JSON
        # boundary after doing all the real math in Decimal.
        "body": json.dumps(body, default=str) if body is not None else "",
    }


def _fetch_coinbase_price():
    """Isolated on its own so tests can monkeypatch just the network call."""
    with urllib.request.urlopen(COINBASE_URL, timeout=5) as resp:
        payload = json.loads(resp.read())
    # rates.USD comes back as a string (e.g. "62876.95"), not a number —
    # confirmed against the live endpoint. Casting here, once, is what keeps
    # that string from leaking into btc_amount = usd_amount / price math.
    return Decimal(payload["data"]["rates"]["USD"])


def get_btc_price():
    """Returns (price, stale). A short TTL cache means normal traffic and
    tests don't hammer Coinbase; a failed live fetch falls back to the last
    known price (flagged stale) instead of ever crashing the transaction
    flow — only a cold start with zero cache history and a failed fetch
    raises PriceUnavailableError."""
    now = time.time()
    cached_price = _price_cache["price"]
    if cached_price is not None and now - _price_cache["fetched_at"] < PRICE_CACHE_TTL_SECONDS:
        return cached_price, False

    try:
        price = _fetch_coinbase_price()
        _price_cache["price"] = price
        _price_cache["fetched_at"] = now
        return price, False
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, decimal.InvalidOperation):
        if cached_price is not None:
            return cached_price, True
        raise PriceUnavailableError(
            "Coinbase price feed is unavailable and no cached price exists yet"
        )


def lambda_handler(event, context):
    method = event.get("httpMethod")
    # API Gateway's proxy event carries the matched route template in
    # `resource` (e.g. "/accounts/{account_id}/balance/verify") — needed
    # because several routes share the same method + path-param shape and
    # can't be told apart from pathParameters alone.
    resource = event.get("resource") or event.get("path") or ""
    path_params = event.get("pathParameters") or {}
    account_id = path_params.get("account_id")

    try:
        if resource == "/accounts" and method == "POST":
            return create_account(event)
        if resource == "/accounts/{account_id}" and method == "GET":
            return get_account(account_id)
        if resource == "/accounts/{account_id}/balance/verify" and method == "GET":
            return verify_balance(account_id)
        if resource == "/accounts/{account_id}/transactions" and method == "POST":
            return create_transaction(account_id, event)
        if resource == "/accounts/{account_id}/transactions" and method == "GET":
            return list_transactions(account_id)
        if resource == "/price" and method == "GET":
            return get_price()
        return _response(400, {"message": "Unsupported route"})
    except ValueError as exc:
        return _response(400, {"message": str(exc)})
    except PriceUnavailableError as exc:
        return _response(503, {"message": str(exc)})


def _default_seed_balances():
    # Non-round, realistic-looking demo seed values (spec's front-end
    # section) — random.uniform is fine here, this randomness never touches
    # actual trade math, only the cosmetic starting balance.
    usd_balance = Decimal(str(round(random.uniform(25000, 50000), 2)))
    btc_balance = _quantize_btc(Decimal(str(random.uniform(0.5, 1.5))))
    return usd_balance, btc_balance


def create_account(event):
    body = json.loads(event.get("body") or "{}")

    if "usd_balance" in body or "btc_balance" in body:
        usd_balance = Decimal(str(body.get("usd_balance", "0")))
        # Quantized even on an explicit, caller-supplied value — nothing
        # anywhere in the system should ever hold a btc_balance with more
        # than satoshi precision, not just the randomly-generated default.
        btc_balance = _quantize_btc(Decimal(str(body.get("btc_balance", "0"))))
    else:
        usd_balance, btc_balance = _default_seed_balances()

    if usd_balance < 0 or btc_balance < 0:
        raise ValueError("seed balances must not be negative")

    display_name = body.get("display_name", "Demo Account")
    account_id = str(uuid.uuid4())
    seed_transaction_id = str(uuid.uuid4())
    now = _now_iso()

    # Account creation and its SEED ledger row are one atomic write — the
    # transaction log (including this row) is the only source of truth for
    # balances (rule 4); there is no separate, untracked "starting balance"
    # field anywhere else to drift from it.
    dynamodb_client.transact_write_items(
        TransactItems=[
            {
                "Put": {
                    "TableName": ACCOUNTS_TABLE_NAME,
                    "Item": _to_dynamo(
                        {
                            "account_id": account_id,
                            "usd_balance": usd_balance,
                            "btc_balance": btc_balance,
                            "created_at": now,
                            "display_name": display_name,
                        }
                    ),
                }
            },
            {
                "Put": {
                    "TableName": TRANSACTIONS_TABLE_NAME,
                    "Item": _to_dynamo(
                        {
                            "transaction_id": seed_transaction_id,
                            "account_id": account_id,
                            "type": "SEED",
                            "usd_amount": usd_balance,
                            "btc_amount": btc_balance,
                            "status": "EXECUTED",
                            "executed_at": now,
                        }
                    ),
                }
            },
        ]
    )

    return _response(
        201,
        {
            "account_id": account_id,
            "usd_balance": usd_balance,
            "btc_balance": btc_balance,
            "created_at": now,
            "display_name": display_name,
        },
    )


def get_account(account_id):
    account = accounts_table.get_item(Key={"account_id": account_id}).get("Item")
    if not account:
        return _response(404, {"message": "Account not found"})
    return _response(200, account)


def _query_account_transactions(account_id):
    items = []
    kwargs = {
        "IndexName": "AccountIndex",
        "KeyConditionExpression": Key("account_id").eq(account_id),
    }
    while True:
        resp = transactions_table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


def list_transactions(account_id):
    account = accounts_table.get_item(Key={"account_id": account_id}).get("Item")
    if not account:
        return _response(404, {"message": "Account not found"})
    transactions = _query_account_transactions(account_id)
    transactions.sort(key=lambda t: t["executed_at"])
    return _response(200, transactions)


def verify_balance(account_id):
    account = accounts_table.get_item(Key={"account_id": account_id}).get("Item")
    if not account:
        return _response(404, {"message": "Account not found"})

    computed_usd = Decimal("0")
    computed_btc = Decimal("0")
    for txn in _query_account_transactions(account_id):
        if txn["type"] == "SEED":
            computed_usd += txn["usd_amount"]
            computed_btc += txn["btc_amount"]
        elif txn["status"] != "EXECUTED":
            continue  # rejected attempts never count toward the ledger (rule 4)
        elif txn["type"] == "BUY":
            computed_usd -= txn["usd_amount"]
            computed_btc += txn["btc_amount"]
        elif txn["type"] == "SELL":
            computed_usd += txn["usd_amount"]
            computed_btc -= txn["btc_amount"]

    cached_usd = account["usd_balance"]
    cached_btc = account["btc_balance"]

    return _response(
        200,
        {
            "account_id": account_id,
            "cached_usd_balance": cached_usd,
            "cached_btc_balance": cached_btc,
            "computed_usd_balance": computed_usd,
            "computed_btc_balance": computed_btc,
            "matches": computed_usd == cached_usd and computed_btc == cached_btc,
        },
    )


def _record_rejected_insufficient_funds(transaction_id, account_id, trade_type, usd_amount, price, btc_amount, now):
    # A standalone Put, not part of the failed TransactWriteItems (which
    # rolled back completely) — this is the one case a REJECTED_* row can
    # legitimately use the client's own transaction_id as its PK, since
    # nothing else has claimed that key yet. Still guarded by the same
    # idempotency condition: if two concurrent attempts on this exact ID
    # both hit insufficient funds, only one gets to record the outcome: the
    # other's write loses the race and is discarded here, not treated as an
    # error, since the caller's own retry-as-duplicate path already handles
    # the same-ID replay case.
    try:
        transactions_table.put_item(
            Item={
                "transaction_id": transaction_id,
                "account_id": account_id,
                "type": trade_type,
                "usd_amount": usd_amount,
                "btc_price_at_execution": price,
                "btc_amount": btc_amount,
                "status": "REJECTED_INSUFFICIENT_FUNDS",
                "executed_at": now,
            },
            ConditionExpression="attribute_not_exists(transaction_id)",
        )
    except transactions_table.meta.client.exceptions.ConditionalCheckFailedException:
        pass


def create_transaction(account_id, event):
    body = json.loads(event.get("body") or "{}")
    transaction_id = body.get("transaction_id")
    trade_type = body.get("type")
    usd_amount = body.get("usd_amount")

    if not transaction_id:
        raise ValueError("'transaction_id' is required")
    if trade_type not in ("BUY", "SELL"):
        raise ValueError("'type' must be 'BUY' or 'SELL'")
    if usd_amount is None:
        raise ValueError("'usd_amount' is required")
    usd_amount = Decimal(str(usd_amount))
    if usd_amount <= 0:
        raise ValueError("'usd_amount' must be a positive amount")

    account = accounts_table.get_item(Key={"account_id": account_id}).get("Item")
    if not account:
        return _response(404, {"message": "Account not found"})

    price, price_stale = get_btc_price()
    btc_amount = _quantize_btc(usd_amount / price)
    now = _now_iso()

    if trade_type == "BUY":
        balance_condition = "usd_balance >= :amt"
        balance_update = "SET usd_balance = usd_balance - :amt, btc_balance = btc_balance + :btc"
        insufficient_field = "usd_balance"
    else:
        balance_condition = "btc_balance >= :btc"
        balance_update = "SET usd_balance = usd_balance + :amt, btc_balance = btc_balance - :btc"
        insufficient_field = "btc_balance"

    transact_items = [
        {
            "Put": {
                "TableName": TRANSACTIONS_TABLE_NAME,
                "Item": _to_dynamo(
                    {
                        "transaction_id": transaction_id,
                        "account_id": account_id,
                        "type": trade_type,
                        "usd_amount": usd_amount,
                        "btc_price_at_execution": price,
                        "btc_amount": btc_amount,
                        "status": "EXECUTED",
                        "executed_at": now,
                    }
                ),
                "ConditionExpression": "attribute_not_exists(transaction_id)",
            }
        },
        {
            "Update": {
                "TableName": ACCOUNTS_TABLE_NAME,
                "Key": _to_dynamo({"account_id": account_id}),
                "UpdateExpression": balance_update,
                "ConditionExpression": balance_condition,
                "ExpressionAttributeValues": {
                    ":amt": _serializer.serialize(usd_amount),
                    ":btc": _serializer.serialize(btc_amount),
                },
            }
        },
    ]

    # The ledger row and the balance update either both land or neither does
    # (rule 3) — and the balance check is part of the same atomic write, not
    # a separate read-then-write, so two concurrent requests can't both pass
    # the check and overdraw the account (rule 2). Two genuinely simultaneous
    # requests touching the same Accounts item can also make DynamoDB itself
    # cancel one with TransactionConflict — a "someone else is mid-write to
    # this item right now" signal, not a business-rule rejection — so that
    # case is retried rather than surfaced as an error.
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            dynamodb_client.transact_write_items(TransactItems=transact_items)
            break
        except dynamodb_client.exceptions.TransactionCanceledException as exc:
            reasons = exc.response.get("CancellationReasons", [])
            # CancellationReasons is positional, matching TransactItems
            # order: index 0 is the ledger Put (idempotency), index 1 is the
            # balance Update (funds check). Untouched items report "None".
            if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                return _response(
                    409, {"status": "REJECTED_DUPLICATE", "message": "transaction_id already used"}
                )
            if len(reasons) > 1 and reasons[1].get("Code") == "ConditionalCheckFailed":
                _record_rejected_insufficient_funds(
                    transaction_id, account_id, trade_type, usd_amount, price, btc_amount, now
                )
                return _response(
                    422,
                    {
                        "status": "REJECTED_INSUFFICIENT_FUNDS",
                        "message": f"insufficient {insufficient_field}",
                    },
                )
            if any(r.get("Code") == "TransactionConflict" for r in reasons) and attempt < max_attempts - 1:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise

    return _response(
        201,
        {
            "transaction_id": transaction_id,
            "account_id": account_id,
            "type": trade_type,
            "usd_amount": usd_amount,
            "btc_price_at_execution": price,
            "btc_amount": btc_amount,
            "status": "EXECUTED",
            "executed_at": now,
            "price_stale": price_stale,
        },
    )


def get_price():
    price, stale = get_btc_price()
    return _response(200, {"btc_usd": price, "stale": stale})
