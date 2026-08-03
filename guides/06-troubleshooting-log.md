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

## The pattern across both parts

Same lesson twice, in two different layers: a green result (a passing
`make test-local-invoke`, a `TransactionCanceledException` that looks like
a permissions problem) isn't proof of anything until you've checked what it
actually verified. Reproducing each failure in isolation — a minimal script
outside the app, a debug print of the actual container environment, a
direct `docker logs` check of whether DynamoDB Local ever received the
request at all — is what turned every vague symptom in both parts into a
precise, one-line root cause.
