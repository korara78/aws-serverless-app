# Guide 1: Project Overview and Architecture

**Last updated:** 2026-08-03

## What this project is

A small serverless CRUD API — API Gateway → Lambda → DynamoDB, built with AWS
SAM — for managing an `Items` resource. The API itself is deliberately simple.
What the project actually demonstrates is the pipeline around it: a complete,
working path from a git push to a verified live deployment, with no manual
steps and no long-lived AWS credentials anywhere.

## Why this project

Three things are easy to claim and hard to actually prove in a portfolio
project: that the tests are real (not testing a mock of a mock), that the
deploy is real (not "should work"), and that the deploy is provably safe to
trigger automatically. This project is built to make all three checkable,
not just asserted:

- **Real tests, not just mocks.** Unit tests use `moto` (fully mocked,
  fast), but integration tests run against a real `amazon/dynamodb-local`
  container over a real network connection — genuine DynamoDB engine
  behavior, not a Python dict pretending to be one.
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
| Docker / SAM CLI local invoke | `Makefile`'s `test-local-invoke` target, `events/list-items.json`, `env.local-invoke.json` |
| Pytest suite | `tests/unit/`, `tests/integration/` |
| Serverless Backend | `template.yaml`, `src/app.py` |
| Post-deploy smoke test | `tests/smoke/test_smoke.py` |

## What the API actually does

Five routes, one Lambda function, one DynamoDB table:

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/items` | Create an item; requires `name` in the JSON body |
| `GET` | `/items` | List all items |
| `GET` | `/items/{id}` | Get one item, `404` if it doesn't exist |
| `PUT` | `/items/{id}` | Update an item, `404` if it doesn't exist |
| `DELETE` | `/items/{id}` | Delete an item, `404` if it doesn't exist |

See `src/app.py` for the full handler — it's short enough to read directly
rather than needing a separate walkthrough.

## Guide series

1. **Project overview and architecture** (this guide)
2. [Environment setup](02-environment-setup.md)
3. [Testing strategy](03-testing-strategy.md)
4. [CI/CD pipeline](04-cicd-pipeline.md)
5. [GitHub OIDC deploy setup](05-github-oidc-deploy-setup.md)
6. [Troubleshooting log](06-troubleshooting-log.md)
