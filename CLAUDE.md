# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A minimal AWS SAM serverless CRUD API — API Gateway + a single Lambda function backed by DynamoDB, managing an `Items` resource (`id`, `name`, arbitrary extra fields). It exists primarily to demonstrate a full serverless CI/CD pipeline: GitHub Actions builds the SAM app, smoke-checks it via a real `sam local invoke` against a Dockerized DynamoDB, runs the pytest suite (unit + integration), deploys only if all of that passes, then runs a separate post-deploy smoke test against the live, deployed API Gateway endpoint.

## Commands

```bash
make build              # sam build
make test-unit          # pytest tests/unit (moto-mocked DynamoDB, fast, no Docker)
make test-integration   # pytest tests/integration (spins up amazon/dynamodb-local in Docker)
make test               # both of the above
make test-local-invoke  # sam local invoke against the built artifact, wired to a
                         # Docker-networked DynamoDB Local (mirrors the CI smoke-check step)
make deploy              # sam deploy (uses samconfig.toml defaults)
make test-smoke          # pytest tests/smoke — requires API_BASE_URL env var pointing
                          # at a live, deployed API Gateway endpoint
```

Setup (not yet installed on this machine as of scaffolding — Docker and Python 3 are present, AWS CLI and SAM CLI are not):

```bash
pip install aws-sam-cli
pip install --break-system-packages -r requirements-dev.txt   # or use a venv
# AWS CLI: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
```

## Architecture

- `template.yaml` — SAM template: `ItemsTable` (DynamoDB, `id` as partition key, on-demand billing), `ItemsFunction` (Python 3.12 Lambda, `DynamoDBCrudPolicy` scoped to the table), and five API Gateway routes (`POST /items`, `GET /items`, `GET /items/{id}`, `PUT /items/{id}`, `DELETE /items/{id}`).
- `src/app.py` — single handler (`lambda_handler`) that routes on `httpMethod` + presence of `pathParameters.id`. Reads `TABLE_NAME` from the environment (set via the template's `Globals`). Also honors an optional `DYNAMODB_ENDPOINT_URL` env var to point boto3 at a local DynamoDB endpoint instead of real AWS — this is the hook that makes local/integration testing and the `sam local invoke` smoke check possible without touching a real account.
- `tests/unit/test_app.py` — exercises `app.lambda_handler` directly against a `moto`-mocked DynamoDB table. No Docker, no network, sub-second.
- `tests/integration/` — `conftest.py` starts a real `amazon/dynamodb-local` Docker container (session-scoped) and creates/tears down a uniquely-named table per test via the `items_table` fixture; `test_api_integration.py` runs the full create → list → update → delete → 404 lifecycle against that real (if local) DynamoDB engine, `importlib.reload`-ing `app` after `monkeypatch`-setting env vars so the module rebinds its boto3 resource to the local endpoint.
- `tests/smoke/test_smoke.py` — same CRUD lifecycle, but via `requests` against `API_BASE_URL` (the real deployed API Gateway stage) — confirms the live endpoint works, not just the local stand-in.
- `Makefile` — the single source of truth for how each step is actually run; both local dev and CI (`.github/workflows/ci-cd.yml`) call into it so the two never drift.
- `events/list-items.json` + `env.local-invoke.json` — inputs for `make test-local-invoke`: a canned `GET /items` API Gateway proxy event, and an env-var override file (`--env-vars`) pointing `ItemsFunction` at `http://dynamodb-local:8000` — the container name/network (`sam-test-net`) that `make test-local-invoke` itself creates, so the invoked Lambda container can reach the DynamoDB Local container by Docker DNS.
- `.github/workflows/ci-cd.yml` — runs on PRs and pushes to `main`. Every run: `make build` → `make test-local-invoke` → `make test`. Only on a push to `main` does it additionally configure AWS credentials (via OIDC role assumption — no long-lived keys), `sam deploy`, look up the stack's `ApiEndpoint` output, and run `make test-smoke` against it. Guarded by a `concurrency` group so overlapping runs can't hit the same stack at once.
- `samconfig.toml` — default `sam deploy` parameters (stack name `aws-serverless-app`, `us-east-1`, `CAPABILITY_IAM`, S3 bucket auto-resolved).

## Notes

- The CI deploy step expects two repo secrets that are **not yet configured**: `AWS_DEPLOY_ROLE_ARN` (an OIDC-trusted IAM role, not access keys) and `AWS_REGION`. Deploys will no-op/fail until those exist.
- No git repository has been initialized for this project yet.
- Local dev happens in WSL2/Ubuntu, matching CI's `ubuntu-latest` runner.
