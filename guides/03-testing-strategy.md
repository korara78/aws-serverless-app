# Guide 3: Testing Strategy

**Last updated:** 2026-08-03

This project deliberately uses four different layers of testing, each
proving a different thing. None of them is redundant with the others —
each one would let a different class of bug through if it were removed.

## Layer 1: Unit tests (`tests/unit/`)

Exercise `app.lambda_handler` directly against a `moto`-mocked DynamoDB
(`mock_aws()`). No Docker, no network, no AWS account — sub-second, runs
anywhere. These cover the routing logic and the business rules that don't
need real concurrency to verify: idempotent duplicate rejection,
insufficient-funds rejection (and that the balance is unchanged after),
reconciliation matching for a fresh account and after several transactions,
reconciliation catching an injected mismatch, append-only enforcement (no
update/delete route exists at all), boundary math (exact-balance trades,
fractional BTC amounts), and the Coinbase price feed's failure/staleness
handling — including a regression test for the exact bug caught verifying
the live endpoint (`rates.USD` comes back as a string, not a number).

The Coinbase call itself is never real here: `app._fetch_coinbase_price` —
the one function that actually hits the network — is monkeypatched to a
fixed price in every test that needs deterministic math, so BUY/SELL
amounts are exact and reproducible. What this layer *can't* prove: that the
code's assumptions about DynamoDB's actual API behavior are correct, or
that anything works under genuine concurrent load — `moto` runs
single-threaded, in-process, so two "concurrent" requests in a unit test
are still strictly sequential from DynamoDB's point of view.

## Layer 2: Integration tests (`tests/integration/`)

`tests/integration/conftest.py` starts a real `amazon/dynamodb-local`
Docker container (session-scoped fixture) and creates uniquely-named
`Accounts`/`Transactions` tables (with the `AccountIndex` GSI) per test run
via the `ledger_tables` fixture. `test_api_integration.py` then runs
against that *real* DynamoDB engine — genuine network calls, genuine API
responses, not a mock's approximation of them. Every test uses its own
freshly created account (via a pytest fixture, per the spec's own test-
isolation requirement) — nothing here relies on or mutates another test's
state.

This layer earns its keep with two tests unit tests structurally can't do:

- **A real concurrency race.** `test_concurrent_buy_requests_that_together_exceed_balance`
  fires two `BUY` requests at the same account from two real threads
  (`ThreadPoolExecutor`) simultaneously, for an amount that together
  exceeds the balance. Against `moto` this would just run sequentially and
  prove nothing; against real DynamoDB Local it's an actual race, and the
  test confirms exactly one request succeeds and the balance never goes
  negative — direct proof the atomic conditional write does what rule 2 of
  the spec requires, not just that the code *looks* like it should.
- **One test hits the real Coinbase endpoint**, not a mocked price —
  `test_full_lifecycle_against_real_dynamodb_and_coinbase` — proving the
  whole real chain (DynamoDB Local + a genuine external HTTP call) works
  end to end. Every other integration test fixes the price via the same
  monkeypatch technique as the unit tests, since their point is proving
  DynamoDB Local's transactional behavior, not Coinbase's availability —
  mixing in live price variability there would only add flakiness for zero
  benefit.

**A real bug this layer caught building it:** the original implementation
called `dynamodb.meta.client.transact_write_items(...)` — the low-level
client hanging off a `boto3.resource("dynamodb")`. That client carries
DynamoDB-specific event hooks meant for the *high-level* Table API's
native-Python-type item transformation. Reusing it for `transact_write_items`
— which takes already-low-level-formatted `AttributeValue` items — makes
those hooks try to re-transform items that are already in wire format,
corrupting them (`TransactionCanceledException` with cancellation reason
`TypeError: cannot use 'moto.dynamodb.models.dynamo_type.DynamoType' as a
dict key`). The fix: a separate, independently-created `boto3.client("dynamodb")`
for every transactional write — `src/app.py`'s `dynamodb_client`. Caught
locally with `moto` before this ever reached DynamoDB Local or a real
account; see [Guide 6](06-troubleshooting-log.md) for the full writeup.

**A second real bug this layer caught — but only after the first live
deploy, not before:** `verify_balance` reported false mismatches on
genuinely correct accounts. Root cause was a Python-`Decimal`-vs-DynamoDB
precision mismatch (Python's default context caps at 28 significant
digits, DynamoDB's own Number type preserves up to 38), fixed by
quantizing every BTC-denominated value to satoshi precision (1e-8) at the
one point each enters the system, plus a `TypeSerializer` patch for a
related scientific-notation issue that quantizing exposed. The regression
test for this (`test_reconciliation_matches_with_high_precision_mixed_digit_amounts`)
deliberately lives here, not in `tests/unit` — confirmed moto's own
`UpdateExpression` arithmetic goes through the same Python `Decimal`
context as the test itself, so it can't actually catch this class of bug;
real DynamoDB Local can, and does. Full writeup, including why the first
fix attempt made things worse before finding the real one, in
[Guide 6](06-troubleshooting-log.md).

One implementation detail worth calling out: `src/app.py` creates its
`boto3` resources once, at import time, using whatever table names and
`DYNAMODB_ENDPOINT_URL` are set in the environment at that moment (the
normal, correct pattern for Lambda — you want that setup done once at cold
start, not on every invocation). That means a test can't just
`monkeypatch.setenv()` and expect `app.py`'s already-created resources to
notice. The fix, visible in `test_api_integration.py`:

```python
def _reload_app():
    import app
    importlib.reload(app)
    return app
```

Setting the env vars *then* reloading the module re-runs its top-level
code, rebinding everything to point at the local container. Testing-only
technique — a real Lambda cold-start never needs it.

## Layer 3: `sam local invoke` smoke check (`make test-local-invoke`)

Runs the *actual built Lambda artifact*, inside the *actual Lambda Docker
runtime image* SAM CLI uses — catches packaging problems Layer 2 can't
(missing dependency, a handler path that's wrong in `template.yaml`, a
Python syntax feature the real Lambda runtime doesn't support) since Layer
2 runs `app.lambda_handler` as plain Python in your own local interpreter,
not the Lambda runtime.

The canned event (`events/get-account.json`) is a `GET` on a nonexistent
account — enough to prove the built artifact boots, reads its table-name
env vars, and reaches DynamoDB Local successfully, without needing
Coinbase or any pre-seeded data. `test-local-invoke` creates a Docker
network (`sam-test-net`), runs `amazon/dynamodb-local` on it, and invokes
`sam local invoke --docker-network sam-test-net --env-vars env.local-invoke.json`.

**Two real, non-obvious things had to be fixed to make this layer actually
mean anything** (both found by actually running it locally for the first
time — this exact check had never been run outside CI before):

1. **`sam local invoke --env-vars` only overrides environment variables
   already declared in the template.** `DYNAMODB_ENDPOINT_URL` was only
   ever supplied via `env.local-invoke.json`, never declared in
   `template.yaml`'s `Globals.Function.Environment.Variables` — so SAM CLI
   silently dropped it, the Lambda fell through to its `boto3.resource("dynamodb")`
   default (real AWS), and got `UnrecognizedClientException` from
   authenticating with fake local credentials against a real endpoint.
   Fixed by declaring `DYNAMODB_ENDPOINT_URL: ""` in the template so
   `--env-vars` has an existing key to override; it stays empty (and
   therefore inert — see `app.py`'s `if _endpoint_url:` check) on a real
   deploy.
2. **DynamoDB Local partitions its in-memory data by the AWS access key
   used in each request**, unless started with `-sharedDb`. The
   `aws dynamodb create-table` call in the Makefile and the invoked Lambda
   container (which gets its own default credentials from the SAM CLI
   Lambda runtime emulator, `defaultkey`/`defaultsecret` — unrelated to
   anything in `env.local-invoke.json`, since `AWS_ACCESS_KEY_ID` isn't a
   template-declared variable either) were silently using two different
   credentials, and therefore two different, empty databases —
   `ResourceNotFoundException: Cannot do operations on a non-existent
   table`, for a table that very much existed under different credentials.
   Fixed by starting DynamoDB Local with `-inMemory -sharedDb` in both the
   Makefile and `tests/integration/conftest.py`.

**A third thing worth knowing, not a bug exactly but a real gap:**
`sam local invoke` exits `0` even when the invoked function throws an
unhandled exception — a synchronous Lambda invoke "succeeding" just means
the platform ran the function and returned *a* response, error payload or
not, mirroring real Lambda's own invoke semantics. That means the exit code
alone was never actually a valid pass/fail signal for this smoke check —
before the fixes above, `make test-local-invoke` was passing (exit 0)
while the invoked function was silently throwing on every single run. The
target now `tee`s the response to a file and greps it for `"errorType"`,
failing the `make` target explicitly if the function's own response
indicates an error, rather than trusting SAM CLI's exit code.

## Layer 4: Post-deploy smoke test (`tests/smoke/`)

Runs only in CI, only after a real `sam deploy` succeeds. `test_smoke.py`
uses plain `requests` to run a full account lifecycle (create → check the
price feed → `BUY` → confirm the duplicate `transaction_id` is rejected →
verify reconciliation → confirm history) against `API_BASE_URL` — the
actual API Gateway URL from the just-deployed stack's CloudFormation
outputs. This is the layer that answers the only question that actually
matters at the end of a deploy: does the live thing work, right now, for
real — not "did the previous three layers pass," which only proves the
*code* is probably fine. There's no delete-account endpoint (deliberate —
real ledgers don't delete, and `Transactions` rows are append-only by
design), so accounts created here accumulate in the live environment, same
as every other suite in this portfolio that creates real records against a
live target rather than cleaning up after itself.

**A real, honest gap this layer has:** `requests` (like every layer above
it) doesn't enforce CORS — only an actual browser does. A CORS
misconfiguration between the API and the frontend (see Guide 6 Part 4 for
the real one this project shipped: a preflight that succeeds while the
actual response is missing the required header) is invisible to all four
layers here, `test_smoke.py` included, since none of them is a browser
making a cross-origin request. The only thing that actually caught it was
opening the live dashboard in a real browser after deploying. That's a
genuine blind spot in this test suite, not a solved problem — a headless-
browser check against the live frontend would close it, and isn't built.

## Summary

| Layer | Real DynamoDB? | Real Lambda runtime? | Real network? | Real deployed endpoint? |
|---|---|---|---|---|
| Unit | No (mocked) | No | No | No |
| Integration | Yes (local) | No | Yes (local + 1 real Coinbase call) | No |
| `sam local invoke` | Yes (local) | Yes | Yes (local) | No |
| Smoke | Yes (AWS) | Yes | Yes (internet) | Yes |
