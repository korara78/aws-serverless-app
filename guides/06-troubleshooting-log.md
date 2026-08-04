# Guide 6: Troubleshooting Log

**Last updated:** 2026-08-03

Two separate rounds of real issues, for two separate reasons: getting the
original CRUD scaffold's *deploy pipeline* from red to green (Part 1,
OIDC/IAM), and getting the ledger migration's *local dev/testing tooling*
actually correct rather than silently passing while broken (Part 2). None
of either were guessed at — each was traced through an actual log, AWS
CloudTrail/CloudFormation event, or a direct repro script before being
fixed. Keeping this log rather than cleaning it up afterward, because this
— vague error, find the actual cause, fix the specific thing, verify, move
on — is what building this stuff actually looks like.

---

## Part 1: Getting the deploy pipeline working

The build, the `sam local invoke` smoke check, and the full unit +
integration pytest suite all passed on the very first real CI run. Getting
the *deploy* step from red to green took four distinct, real issues, found
in this order, each one only after the previous one was actually fixed and
pushed.

---

### 1. Missing `id-token: write` permission

**Symptom:**
```
Error: Credentials could not be loaded, please check your action inputs: Could not load credentials from any providers
```
right at the `Configure AWS credentials` step, despite the role ARN and
region secrets both being set correctly.

**Cause:** `aws-actions/configure-aws-credentials` needs to request a
GitHub-signed OIDC token to exchange for AWS credentials, and GitHub only
issues that token to a job that's explicitly granted `id-token: write`.
Without it, there's no token to exchange at all — the error message just
doesn't say that directly, so it reads like a role/secret configuration
problem instead of a missing-permission one.

**Fix:**
```yaml
jobs:
  build-and-test:
    permissions:
      id-token: write
      contents: read
```

---

### 2. GitHub OIDC sub claim format

**Symptom:**
```
Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```
— despite the trust policy's `sub` condition looking correct
(`repo:korara78/aws-serverless-app:ref:refs/heads/main`), and both the
OIDC provider and the role visibly existing with the right ARNs.

**Cause:** found by reading the actual failed request directly, rather
than re-guessing the trust policy:
```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity \
  --max-results 5
```
The event's `userIdentity.userName` was
`repo:korara78@79677336/aws-serverless-app@1320920972:ref:refs/heads/main`
— GitHub embeds immutable organization/repository IDs in the `sub` claim
alongside the human-readable names, not just the plain
`repo:org/repo:ref:...` format shown in most quick-start examples. A trust
policy written with just the plain names has a condition that simply never
matches anything a real token will ever contain.

**Fix:** look up the real IDs and match on the full format:
```bash
gh api users/korara78 --jq .id        # 79677336
gh api repos/korara78/aws-serverless-app --jq .id   # 1320920972
```
```yaml
StringLike:
  token.actions.githubusercontent.com:sub: "repo:korara78@79677336/aws-serverless-app@1320920972:ref:refs/heads/main"
```

---

### 3. Missing permission for the Serverless transform

**Symptom:** OIDC assumption now succeeded, but `sam deploy` failed with:
```
Error: Failed to create managed resources: Waiter ChangeSetCreateComplete failed: ... matched expected path: "FAILED"
```
Digging into the actual changeset (`aws cloudformation list-change-sets
--stack-name aws-sam-cli-managed-default`) showed the real reason:
```
User: .../aws-serverless-app-github-actions-deploy is not authorized to perform:
cloudformation:CreateChangeSet on resource:
arn:aws:cloudformation:us-east-1:aws:transform/Serverless-2016-10-31
```

**Cause:** this looked like an app-specific permission gap, but it's
actually about SAM CLI's own `--resolve-s3` bootstrap stack
(`aws-sam-cli-managed-default`, which creates the S3 bucket deployment
artifacts get uploaded to) — that bootstrap template itself uses
`Transform: AWS::Serverless-2016-10-31`, so the deploy role needs rights to
that transform before it can even create *this* stack, separately from
whatever `template.yaml`'s own transform usage requires.

**Fix:** grant `cloudformation:CreateChangeSet` on the transform's own
(AWS-owned, account-agnostic) ARN:
```yaml
Resource:
  - "arn:aws:cloudformation:*:aws:transform/Serverless-2016-10-31"
```

---

### 4. Missing S3 tagging/delete permissions, and a wedged stack

**Symptom:** past the transform fix, `sam deploy` failed again:
```
Error: Failed to create managed resources: Waiter StackCreateComplete failed:
... matched expected path: "ROLLBACK_FAILED"
```
`aws cloudformation describe-stack-events --stack-name aws-sam-cli-managed-default`
showed two separate `AccessDenied` errors in sequence: bucket creation
failed on `s3:TagResource`, and the automatic rollback that followed then
*also* failed, on `s3:DeleteBucket` — leaving the stack stuck in
`ROLLBACK_FAILED` with an orphaned (but empty) S3 bucket.

**Cause:** the original policy granted `CreateBucket`/`PutObject`/etc. but
not the tagging or deletion actions SAM CLI's bootstrap template actually
uses.

**Fix:** add both, then clean up the wedged stack once the permissions
were actually correct (see
[Guide 5](05-github-oidc-deploy-setup.md#if-the-deploy-roles-bootstrap-s3-stack-ever-gets-stuck)
for the general recovery steps):
```yaml
- s3:TagResource
- s3:UntagResource
- s3:PutBucketTagging
- s3:DeleteBucket
```
```bash
aws cloudformation delete-stack --stack-name aws-sam-cli-managed-default --profile <profile> --region us-east-1
aws cloudformation wait stack-delete-complete --stack-name aws-sam-cli-managed-default --profile <profile> --region us-east-1
```

This actually happened *twice* — once after fix #3 revealed fix #4's gap,
and the stack needed clearing both times before the next `sam deploy`
attempt could proceed.

---

## The pattern across Part 1

Every one of these had a surface error pointing at the wrong layer: a
missing-permission error that read like a credentials error, an
`AccessDenied` that read like a typo'd ARN, a waiter-timeout message that
buried the actual `AccessDenied` two API calls deep. The fix each time was
the same: stop guessing at the workflow YAML, and go read the actual
CloudTrail event or CloudFormation stack event describing exactly which
principal was denied exactly which action on exactly which resource. Four
fixes later, pushed and verified with `gh run watch --exit-status`, the
pipeline went fully green end to end — build, local invoke, tests, OIDC
auth, deploy, and a live smoke test against the real endpoint.

---

## Part 2: Getting the ledger migration's local tooling actually correct

Migrating from the `Items` CRUD scaffold to the real ledger (`Accounts` +
append-only `Transactions`, idempotent/atomic `TransactWriteItems`,
Coinbase price feed) surfaced three real issues — two of them not bugs in
the *business logic* at all, but in the local verification tooling
silently not proving what it looked like it was proving. Found by actually
running `sam` locally for the first time on this machine (it had never
been installed here before) and refusing to trust a green result without
checking what it actually verified.

---

### 5. `boto3.resource().meta.client` corrupts `TransactWriteItems` payloads

**Symptom:** every account-creation and transaction call failed instantly:
```
TransactionCanceledException: Transaction cancelled, please refer
cancellation reasons for specific reasons [TypeError, TypeError]
CancellationReasons: [{'Code': 'TypeError', 'Message': "cannot use
'moto.dynamodb.models.dynamo_type.DynamoType' as a dict key (unhashable
type: 'dict')"}]
```
against `moto` — before this ever touched DynamoDB Local or a real account.

**Cause:** the code called `dynamodb.meta.client.transact_write_items(...)`,
where `dynamodb = boto3.resource("dynamodb")`. A resource's `.meta.client`
carries DynamoDB-specific event hooks meant for the *high-level* Table
API's automatic native-Python-type ↔ `AttributeValue` conversion.
`transact_write_items` is a low-level operation that takes items already in
`AttributeValue` wire format (`{"S": "..."}`, `{"N": "..."}`) — those hooks
tried to re-transform items that were already transformed, producing
garbage. Confirmed by reproducing the exact same call against a plain
`boto3.client("dynamodb")` (works) versus `boto3.resource("dynamodb").meta.client`
(fails identically) with nothing else different.

**Fix:** a completely separate, independently-created low-level client for
every transactional write:
```python
dynamodb = boto3.resource("dynamodb")           # for get_item/query — Table API
dynamodb_client = boto3.client("dynamodb")       # for transact_write_items only
```

---

### 6. `sam local invoke --env-vars` can't inject undeclared variables

**Symptom:** `sam local invoke` against the built Lambda, wired to a local
DynamoDB Local container via `env.local-invoke.json`'s `DYNAMODB_ENDPOINT_URL`,
failed with:
```
UnrecognizedClientException: The security token included in the request is invalid.
```
— the exact error you'd get authenticating with fake credentials against
*real* AWS, not the local container.

**Cause:** dumping the invoked container's actual environment (`os.environ`)
showed `DYNAMODB_ENDPOINT_URL` simply wasn't present at all, and
`AWS_ACCESS_KEY_ID` was SAM's own Lambda-runtime-emulator default
(`defaultkey`), not the `local` value supplied via `--env-vars`. SAM CLI's
`--env-vars` only *overrides* environment variables already declared in the
function's `Environment.Variables` in the template — any key in the
`--env-vars` file that isn't already template-declared is silently dropped,
never injected as new. `DYNAMODB_ENDPOINT_URL` had never been declared in
`template.yaml`, only ever supplied via the override file — so it never
existed to override, and the resource fell back to its `if _endpoint_url:`
branch never triggering, i.e. real AWS.

**Fix:** declare it in the template with an empty default, purely so
`--env-vars` has something to override locally:
```yaml
Environment:
  Variables:
    DYNAMODB_ENDPOINT_URL: ""
```
Stays empty (and inert) on a real deploy.

---

### 7. DynamoDB Local partitions data by AWS access key

**Symptom:** past fix #6, a new error: `ResourceNotFoundException: Cannot
do operations on a non-existent table` — for a table that had just been
created seconds earlier and was visibly present via `aws dynamodb
list-tables` against the same endpoint.

**Cause:** DynamoDB Local, by default, isolates its in-memory data per
unique `(access key, region)` pair used in each request — not a single
shared database. The `aws dynamodb create-table` call in the Makefile used
`AWS_ACCESS_KEY_ID=local`; the invoked Lambda container used SAM's own
default `defaultkey` (per issue #6, `AWS_ACCESS_KEY_ID` isn't
template-declared either, so the `local` override in `env.local-invoke.json`
was also silently dropped). Two different credentials meant two different,
independently-empty databases, both reachable at the same endpoint URL.

**Fix:** start DynamoDB Local with `-sharedDb`, making all data live in one
namespace regardless of which credentials each request used:
```bash
docker run ... amazon/dynamodb-local:3.3.1 -jar DynamoDBLocal.jar -inMemory -sharedDb
```
Applied in both the Makefile's `test-local-invoke` target and
`tests/integration/conftest.py` (the latter wasn't actually broken by this
— it happens to use `local`/`local` consistently everywhere already — but
there's no reason to leave the same footgun sitting there for later).

---

### A related, non-obvious gap: `sam local invoke`'s exit code isn't a pass/fail signal

Not a bug fix exactly, but worth recording: while debugging #6 and #7,
`make test-local-invoke` was exiting `0` — "passing" — on every single run,
including every run where the invoked function was throwing an unhandled
exception. `sam local invoke` mirrors real Lambda's own synchronous-invoke
semantics: the platform successfully ran the function and returned *a*
response, error payload or not, and that counts as CLI-level success
regardless of what's inside the response. The `Makefile` target now
captures the response and greps it for `"errorType"`, failing explicitly if
the function's own response indicates an error, instead of trusting the
exit code.

---

## Part 3: A real precision bug, caught by the live smoke test doing its job

PR #1 (the ledger migration) merged, deployed, and its automated post-deploy
smoke test failed on `GET /price` returning `403`. Manually curling the
endpoint minutes later returned a clean `200` — API Gateway's edge
deployment hadn't fully propagated yet when the smoke test fired
immediately after `sam deploy` finished; a transient timing race, not a
code bug. But re-running the full lifecycle by hand to confirm the API
actually worked surfaced something the automated smoke test's simple
assertions never would have: `GET /accounts/{id}/balance/verify` reported
`"matches": false` on an account with completely legitimate transaction
history.

### 8. Reconciliation false-mismatch: Python vs. DynamoDB Decimal precision

**Symptom:** `cached_btc_balance` and `computed_btc_balance` printed as
visually-different strings —
`0.788599963302284761560276665196` (cached) vs.
`0.7885999633022847615602766652` (computed) — 30 significant digits versus
28.

**Cause:** Python's default `Decimal` context caps at 28 significant
digits. DynamoDB's own Number type preserves up to 38, and the balance
update (`SET btc_balance = btc_balance + :btc`) happens server-side, inside
DynamoDB itself — not in Python. Summing a low-precision `SEED` balance
(`0.7870232`, from the random demo-seed generator) with a `BUY`'s computed
`btc_amount` (up to 28 digits from a single division) produced an *exact*
sum needing 30 digits to represent losslessly. DynamoDB kept all 30;
Python's `verify_balance` summation, bound by the default 28-digit context,
silently rounded its copy down to 28 — a false mismatch on genuinely
correct data.

**First fix attempt — wrong:** widen Python's context to match DynamoDB's
own ceiling (`decimal.getcontext().prec = 38`). This resolved the
mismatch, but broke `create_transaction` outright:
```
ValidationException: DynamoDB only supports precision up to 38 digits
```
Because `btc_amount = usd_amount / price` now had *room* to compute a
result using close to the full 38 digits itself, and DynamoDB's own
addition of that value to the existing cached balance could need *more*
than 38 digits to represent exactly — which DynamoDB rejects outright
rather than silently rounding. Raising the ceiling just moved the same
problem one level up.

**The actual fix:** stop treating `btc_amount` as an arbitrary-precision
division result at all. Real BTC amounts are granular to the satoshi
(1e-8) — quantize every BTC-denominated value (a `SEED` balance, a computed
`btc_amount`) to 8 decimal places, with `ROUND_DOWN`, at the one point each
enters the system, before it's used in any arithmetic or stored anywhere.
Bounded inputs keep every subsequent sum well within both Python's default
precision and DynamoDB's ceiling — the mismatch becomes structurally
impossible rather than papered over by raising a limit.

**A second bug, found fixing the first:** quantizing to a fixed number of
decimal places can itself produce a `Decimal` object whose default string
form is scientific notation — `Decimal('0').quantize(Decimal('0.00000001'))`
is `Decimal('0E-8')`, and *any* sufficiently small quantity (not just exact
zero — `Decimal('0.0000001')` renders as `'1E-7'`) hits the same thing.
boto3's `TypeSerializer` converts `Decimal` to a DynamoDB Number using
plain `str()`, and DynamoDB's Number type flatly rejects scientific
notation:
```
ValueError: invalid literal for int() with base 10: '0E-8'
```
(surfacing from *inside* a `TransactWriteItems` call, via moto in local
testing — the same constraint applies against real DynamoDB, this just
happened to be caught locally first this time). Fixed by patching
`TypeSerializer.serialize` at the class level to use `format(value, "f")`
(always fixed-point) instead of `str()` for `Decimal` specifically — done
once, so it covers both this module's own low-level `transact_write_items`
calls *and* the resource-level `Table.put_item`/`update_item` API, which
uses the identical `TypeSerializer` internally. Guarded to only patch once
(`_patched_for_decimal` marker), since `importlib.reload(app)` — used by
the integration tests to rebind `app` to a local DynamoDB endpoint — would
otherwise re-wrap the already-patched function around itself on every
reload and blow the recursion limit on the very next call.

**Why this one wasn't caught by any local test until now:** every existing
unit/integration test used clean, low-digit-count fixture values (round
seed balances, round prices) that never happened to need more than a
handful of significant digits — the bug only manifests when a
low-precision value and a high-precision one get summed together, which
only occurred with the randomly-generated demo seed balance in real use.
The regression test added for this (`test_reconciliation_matches_with_high_precision_mixed_digit_amounts`)
deliberately lives in `tests/integration`, not `tests/unit` — confirmed
that moto's `UpdateExpression` arithmetic goes through the same
process-wide Python `Decimal` context as the test itself, so both
"cached" and "computed" would round identically under moto regardless of
whether the real fix is in place, giving false confidence. Real DynamoDB
Local reproduces the exact discrepancy real AWS does.

---

## Part 4: CORS preflight succeeds, the real request still gets blocked

The frontend deployed cleanly — bucket, distribution, `sam deploy`, the
API's post-deploy smoke test all green. Loading the live dashboard in a
real browser and clicking "Create demo account" still failed outright:

```
Access to fetch at '.../Prod/accounts' from origin 'https://d....cloudfront.net'
has been blocked by CORS policy: No 'Access-Control-Allow-Origin' header
is present on the requested resource.
```

**Cause:** confirmed directly with `curl -X OPTIONS ... -H "Origin: ..."` —
the preflight response came back with `access-control-allow-origin` set
correctly, exactly matching `Globals.Api.Cors.AllowOrigin`. The *actual*
`POST /accounts` that immediately followed it came back with no CORS
header at all. SAM's `Cors` config only auto-generates the OPTIONS
preflight method and its response — with a Lambda proxy integration, API
Gateway passes through exactly whatever headers the Lambda itself returns
for the real request, and `app.py`'s `_response()` never set one. The
browser's preflight check passing is necessary but not sufficient; it
doesn't mean the real response will satisfy CORS too.

**Fix:** pass the frontend's origin into the Lambda as an env var
(`FRONTEND_ORIGIN`, the same `!Sub "https://${FrontendDistribution.DomainName}"`
value already used for the `Cors` config, so there's exactly one place
that domain is computed) and have `_response()` set
`Access-Control-Allow-Origin` on every response, not just rely on API
Gateway's auto-generated preflight to cover it.

**Why no local/CI test caught this before the live check:** every existing
test calls `app.lambda_handler` directly in Python — there's no actual
browser, and therefore no CORS enforcement to violate in the first place.
Enforcement is a browser behavior, not a server behavior; a server can
return a CORS-non-compliant response and nothing about the HTTP exchange
itself is "wrong" from the server's or `curl`'s point of view. The two new
unit tests (`test_response_includes_cors_header_when_frontend_origin_configured`/
`..._omits_cors_header_when_frontend_origin_unset`) can only verify the
Lambda's own output includes the right header — they can't (and don't
try to) simulate actual browser CORS enforcement. That gap is inherent to
testing at this level, not a coverage bug to fix; it's exactly why the
real-browser-against-the-live-deployment check exists as its own step.

---

## The pattern across all four parts

Same lesson four times, in four different layers: a green result (a
passing `make test-local-invoke`, a `TransactionCanceledException` that
looks like a permissions problem, a live smoke test that failed for an
unrelated transient reason, a CORS preflight that succeeds) isn't proof of
anything — good or bad — until you've checked what it actually verified,
or manually re-confirmed the real thing works. Reproducing each failure in
isolation — a minimal script outside the app, a debug print of the actual
container environment, a direct `docker logs` check, comparing exact digit
counts between two "equal-looking" numbers, a raw `curl -X OPTIONS` against
the live endpoint — is what turned every vague symptom across all four
parts into a precise, one-line root cause.
