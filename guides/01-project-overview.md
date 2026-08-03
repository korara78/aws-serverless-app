# Guide 1: Project Overview and Architecture

**Last updated:** 2026-08-03

## What this project is

A simulated brokerage-style Bitcoin + fiat ledger — API Gateway → Lambda →
DynamoDB, built with AWS SAM. It started as a deliberately trivial `Items`
CRUD API whose only job was to prove the pipeline around it worked; the
business logic layer was later replaced with a real ledger (fiat balance,
BTC balance, buy/sell against a live price, full transaction integrity)
while keeping the exact same SAM + Lambda + DynamoDB + API Gateway stack
and CI/CD pipeline. What the project demonstrates is both things at once: a
complete, working path from git push to verified live deployment, *and*
correctness properties (idempotency, atomicity, no negative balances under
real concurrency) that only matter once the application underneath is
actually worth getting right.

## Why this project

Three things are easy to claim and hard to actually prove in a portfolio
project: that the tests are real (not testing a mock of a mock), that the
deploy is real (not "should work"), and that the business logic is
correct under conditions that are annoying to set up (real concurrency,
real external dependencies, real partial-failure scenarios). This project
is built to make all three checkable, not just asserted:

- **Real tests, not just mocks.** Unit tests use `moto` (fully mocked,
  fast), but integration tests run against a real `amazon/dynamodb-local`
  container over a real network connection — genuine DynamoDB engine
  behavior, not a Python dict pretending to be one. One integration test
  fires two real, simultaneously-submitted `BUY` requests at the same
  account and confirms the atomic conditional write actually prevents an
  overdraft — something a single-threaded mock can't meaningfully test at
  all (see [Guide 3](03-testing-strategy.md)).
- **Real deploy, not just a green checkmark.** The pipeline's last step is a
  smoke test that hits the actual, live, just-deployed API Gateway endpoint
  over HTTPS — not the local stand-in.
- **Real safety, not just "it worked once."** Deploys authenticate via a
  GitHub OIDC-trusted IAM role scoped to this repo's `main` branch — no AWS
  access keys stored in GitHub at all, and the role's own permissions are
  scoped to this app's resource names rather than broad account access.

## Architecture

```
 GitHub Actions
       |
       v
 Docker / SAM CLI ---- sam local invoke against a Dockerized DynamoDB
       |                (proves the packaged Lambda artifact actually runs,
       |                 not just that the source code imports cleanly)
       v
 Pytest suite --------- unit (moto-mocked) + integration (real DynamoDB Local)
       |
       | deploy only if all of the above passes
       v
 Serverless Backend
 (API Gateway -> Lambda -> DynamoDB)
       |
       | hits the live, deployed endpoint
       v
 Post-deploy smoke test
```

Each box maps directly to a file in this repo:

| Box | File(s) |
|---|---|
| GitHub Actions | `.github/workflows/ci-cd.yml` |
| Docker / SAM CLI local invoke | `Makefile`'s `test-local-invoke` target, `events/get-account.json`, `env.local-invoke.json` |
| Pytest suite | `tests/unit/`, `tests/integration/` |
| Serverless Backend | `template.yaml`, `src/app.py` |
| Post-deploy smoke test | `tests/smoke/test_smoke.py` |

## What the API actually does

Six routes, one Lambda function, two DynamoDB tables (`Accounts`, and an
append-only `Transactions` table with a `account_id`+`executed_at` GSI for
per-account history queries):

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/accounts` | Create an account — writes a seeded `Accounts` row and a synthetic `SEED` `Transactions` row atomically |
| `GET` | `/accounts/{id}` | Current cached balances, `404` if unknown |
| `GET` | `/accounts/{id}/balance/verify` | Recompute the balance from the full transaction log and compare to the cache |
| `POST` | `/accounts/{id}/transactions` | Submit a `BUY`/`SELL`; idempotent on client-generated `transaction_id`, atomic against the balance update |
| `GET` | `/accounts/{id}/transactions` | Full transaction history, chronological |
| `GET` | `/price` | Current BTC/USD price, proxied and short-TTL-cached from Coinbase |

The full business-rule reasoning (idempotency via conditional writes,
atomicity via `TransactWriteItems`, why a `SEED` ledger row exists instead
of an untracked starting-balance field, why two REJECTED_* statuses behave
differently under the hood) lives as comments directly in `src/app.py` —
it's short enough to read directly rather than needing a separate
walkthrough.

## Guide series

1. **Project overview and architecture** (this guide)
2. [Environment setup](02-environment-setup.md)
3. [Testing strategy](03-testing-strategy.md)
4. [CI/CD pipeline](04-cicd-pipeline.md)
5. [GitHub OIDC deploy setup](05-github-oidc-deploy-setup.md)
6. [Troubleshooting log](06-troubleshooting-log.md)
