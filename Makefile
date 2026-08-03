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
# over a shared Docker network so the invoked container can reach it.
test-local-invoke: build
	docker network inspect sam-test-net >/dev/null 2>&1 || docker network create sam-test-net
	docker rm -f dynamodb-local >/dev/null 2>&1 || true
	docker run -d --rm --name dynamodb-local --network sam-test-net -p 8000:8000 amazon/dynamodb-local:3.3.1
	AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local aws dynamodb create-table \
		--table-name sam-local-invoke-smoke-table \
		--attribute-definitions AttributeName=id,AttributeType=S \
		--key-schema AttributeName=id,KeyType=HASH \
		--billing-mode PAY_PER_REQUEST \
		--endpoint-url http://localhost:8000 \
		--region us-east-1
	sam local invoke ItemsFunction \
		--event events/list-items.json \
		--env-vars env.local-invoke.json \
		--docker-network sam-test-net
	docker stop dynamodb-local

deploy:
	sam deploy

test-smoke:
	pytest tests/smoke -v
