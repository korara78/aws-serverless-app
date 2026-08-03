.PHONY: build test-unit test-integration test test-local-invoke deploy test-smoke

build:
	sam build

test-unit:
	pytest tests/unit -v

test-integration:
	pytest tests/integration -v

test: test-unit test-integration

# Exercises the built Lambda artifact through the real SAM/Docker local-invoke
# path (diagram's "Docker / SAM CLI" step), wired to a real local DynamoDB
# over a shared Docker network so the invoked container can reach it. Only
# the Accounts table needs to actually exist — the canned event is a GET on
# a nonexistent account, which never touches Transactions; this step is
# purely "does the built artifact boot and reach DynamoDB," not a
# correctness check (that's what tests/unit and tests/integration are for).
#
# -sharedDb: DynamoDB Local otherwise partitions its in-memory data by the
# AWS access key used in each request. The `aws dynamodb create-table` call
# below and the invoked Lambda container (which gets its own default
# credentials from the SAM CLI Lambda runtime emulator, not from this
# Makefile) don't use the same key — without -sharedDb they'd silently see
# two separate, empty databases and every call would 404 on a table that
# very much exists. Cost real debugging time to track down; don't remove.
#
# `sam local invoke` itself exits 0 even when the invoked function throws an
# unhandled exception (a synchronous Lambda invoke "succeeding" just means
# the platform ran the function and got *a* response, error or not) — so
# the exit code alone can't be trusted as the pass/fail signal here. The
# grep step below inspects the actual response payload for an errorType key.
test-local-invoke: build
	docker network inspect sam-test-net >/dev/null 2>&1 || docker network create sam-test-net
	docker rm -f dynamodb-local >/dev/null 2>&1 || true
	docker run -d --rm --name dynamodb-local --network sam-test-net -p 8000:8000 \
		amazon/dynamodb-local:3.3.1 -jar DynamoDBLocal.jar -inMemory -sharedDb
	AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local aws dynamodb create-table \
		--table-name sam-local-invoke-smoke-accounts \
		--attribute-definitions AttributeName=account_id,AttributeType=S \
		--key-schema AttributeName=account_id,KeyType=HASH \
		--billing-mode PAY_PER_REQUEST \
		--endpoint-url http://localhost:8000 \
		--region us-east-1
	sam local invoke LedgerFunction \
		--event events/get-account.json \
		--env-vars env.local-invoke.json \
		--docker-network sam-test-net | tee /tmp/sam-local-invoke-output.json
	docker stop dynamodb-local
	! grep -q '"errorType"' /tmp/sam-local-invoke-output.json

deploy:
	sam deploy

test-smoke:
	pytest tests/smoke -v
