# Guide 4: CI/CD Pipeline

**Last updated:** 2026-08-03

The whole pipeline lives in `.github/workflows/ci-cd.yml`, and every step in
it just calls a `Makefile` target — see [Guide 2](02-environment-setup.md)
for why that matters (local dev and CI can never quietly drift apart, since
they're running the literal same command).

## Trigger and concurrency

```yaml
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

concurrency:
  group: aws-serverless-app-deploy
  cancel-in-progress: false
```

Every PR and every push to `main` runs the full build-and-test path. Only a
push to `main` goes on to deploy. The `concurrency` group ensures two
overlapping workflow runs can never both try to deploy (or roll back) the
same CloudFormation stack at the same time — `cancel-in-progress: false`
means a second run queues and waits rather than killing the first one
mid-deploy, which could otherwise leave the stack in a half-updated state.

## The job

```yaml
permissions:
  id-token: write
  contents: read
```

This is not optional boilerplate — it's the permission that lets the
workflow request a GitHub-signed OIDC token at all. Without it, the deploy
step fails in a way that looks like a credentials problem but isn't one;
see [Guide 6](06-troubleshooting-log.md) for the actual failure this caused.

Steps, in order:

1. **Checkout, set up Python, install SAM CLI, install test deps** — standard setup.
2. **`make build`** (`sam build`) — packages the Lambda.
3. **`make test-local-invoke`** — the Docker/SAM CLI smoke check described in
   [Guide 3](03-testing-strategy.md). Runs on every PR and push, not just
   pushes to `main` — it's cheap and catches packaging problems before any
   AWS credentials are even involved.
4. **`make test`** — unit + integration tests.
5. **Configure AWS credentials** *(main pushes only)* — OIDC role assumption,
   no stored keys. See [Guide 5](05-github-oidc-deploy-setup.md).
6. **`sam deploy`** *(main pushes only)* — deploys `template.yaml`'s stack.
7. **Get API endpoint** *(main pushes only)* — reads the `ApiEndpoint` output
   straight from the just-updated CloudFormation stack via
   `aws cloudformation describe-stacks`, rather than hardcoding the URL
   anywhere — the URL is only knowable after a real deploy, and hardcoding
   it would silently go stale if the stack were ever recreated.
8. **`make test-smoke`** *(main pushes only)* — the live smoke test, using
   the endpoint from the previous step as `API_BASE_URL`.

Every `*(main pushes only)*` step above is gated with the same condition:

```yaml
if: github.ref == 'refs/heads/main' && github.event_name == 'push'
```

which is deliberately more specific than just checking the branch — a
`pull_request` event targeting `main` also has `github.ref` pointing at the
PR's merge ref in some contexts, so both checks together are what actually
restricts this to "a real push landed on `main`," not "something happened
that's related to `main`."

## Why deploy is gated on the whole test suite passing

This is the actual point of the project, not an incidental detail: a
broken change can get all the way to `git push origin main` and still
never reach the live endpoint, because steps 2–4 run *before* any AWS
credentials are requested. If the build fails, the local-invoke smoke
check fails, or a single pytest test fails, the workflow stops there —
`sam deploy` and everything after it never runs.
