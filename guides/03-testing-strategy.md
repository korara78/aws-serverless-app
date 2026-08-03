# Guide 3: Testing Strategy

**Last updated:** 2026-08-03

This project deliberately uses four different layers of testing, each
proving a different thing. None of them is redundant with the others —
each one would let a different class of bug through if it were removed.

## Layer 1: Unit tests (`tests/unit/`)

Exercise `app.lambda_handler` directly against a `moto`-mocked DynamoDB
table (`mock_aws()`). No Docker, no network, no AWS account — sub-second,
runs anywhere. These cover the handler's routing logic and validation
(missing `name` on create, 404s on unknown IDs, the full CRUD lifecycle)
at the level of "does the code do what it's supposed to do."

What this layer *can't* prove: that the code's assumptions about DynamoDB's
actual API behavior are correct. `moto` is a very good simulation, but it's
still a simulation.

## Layer 2: Integration tests (`tests/integration/`)

`tests/integration/conftest.py` starts a real `amazon/dynamodb-local`
Docker container (session-scoped fixture) and creates a uniquely-named
table per test via the `items_table` fixture. `test_api_integration.py`
then runs the full create → list → update → delete → 404 lifecycle against
that *real* DynamoDB engine — genuine network calls, genuine API responses,
not a mock's approximation of them.

One implementation detail worth calling out: `src/app.py` creates its
`boto3` resource once, at import time, using whatever `TABLE_NAME` and
`DYNAMODB_ENDPOINT_URL` are set in the environment at that moment (this is
the normal, correct pattern for Lambda — you want that setup done once at
cold start, not on every invocation). That means a test can't just
`monkeypatch.setenv()` and expect `app.py`'s already-created `dynamodb`
resource to notice. The fix, visible in `test_api_integration.py`:

```python
def _reload_app():
    import app
    importlib.reload(app)
    return app
```

Setting the env vars *then* reloading the module re-runs its top-level
code, rebinding `dynamodb`/`table` to point at the local container. This
is a testing-only technique — production Lambda cold-starts don't need it,
since the environment is fixed for the life of the execution environment.

## Layer 3: `sam local invoke` smoke check (`make test-local-invoke`)

This is a genuinely different thing from Layer 2, even though both end up
talking to a local DynamoDB. Layer 2 runs `app.lambda_handler` as plain
Python, in the same process as pytest. This layer runs the *actual built
Lambda artifact*, inside the *actual Lambda Docker runtime image* SAM CLI
uses, invoked exactly the way `sam local invoke`/`sam local start-api`
would invoke it in a more realistic local-testing setup.

What this catches that Layer 2 can't: packaging problems. A missing
dependency in `src/requirements.txt`, a handler path that's wrong in
`template.yaml`, a Python syntax feature the Lambda runtime's actual
interpreter version doesn't support — none of these would show up running
`app.lambda_handler` directly in your local Python environment, because
your local environment isn't the Lambda runtime.

To make this container reach a local DynamoDB, `make test-local-invoke`
creates a Docker network (`sam-test-net`), runs `amazon/dynamodb-local`
attached to it, and invokes `sam local invoke --docker-network sam-test-net
--env-vars env.local-invoke.json`, where `env.local-invoke.json` points the
function at `http://dynamodb-local:8000` — the container name, resolved via
Docker's own DNS on that shared network. See `events/list-items.json` for
the canned `GET /items` API Gateway proxy event it invokes with.

## Layer 4: Post-deploy smoke test (`tests/smoke/`)

Runs only in CI, only after a real `sam deploy` succeeds. `test_smoke.py`
uses plain `requests` to run the same CRUD lifecycle against `API_BASE_URL`
— the actual API Gateway URL from the just-deployed stack's CloudFormation
outputs. This is the layer that answers the only question that actually
matters at the end of a deploy: does the live thing work, right now, for
real — not "did the previous three layers pass," which only proves the
*code* is probably fine.

## Summary

| Layer | Real DynamoDB? | Real Lambda runtime? | Real network? | Real deployed endpoint? |
|---|---|---|---|---|
| Unit | No (mocked) | No | No | No |
| Integration | Yes (local) | No | Yes (local) | No |
| `sam local invoke` | Yes (local) | Yes | Yes (local) | No |
| Smoke | Yes (AWS) | Yes | Yes (internet) | Yes |
