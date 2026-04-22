.PHONY: up down db-migrate-compose db-upgrade db-current db-history db-revision db-seed db-seed-reset k8s-migrate-dev k8s-migrate-prod qa-unit qa-unit-fast qa-unit-integration qa-e2e qa-evidence qa-evidence-build

up:
	docker compose up --build

down:
	docker compose down

db-migrate-compose:
	docker compose run --rm migrate

db-upgrade:
	python -m app.db.migrate upgrade

db-current:
	python -m app.db.migrate current --verbose

db-history:
	python -m app.db.migrate history --verbose

# usage: make db-revision m="add inventory adjustments"
db-revision:
	python -m app.db.migrate revision -m "$(m)"

db-seed:
	python -m app.db.seed

db-seed-reset:
	python -m app.db.seed --wipe

k8s-migrate-dev:
	kubectl delete job gs-inv-migrate -n gs-inv-dev --ignore-not-found=true
	kubectl apply -k k8s/jobs/overlays/dev
	kubectl wait --for=condition=complete job/gs-inv-migrate -n gs-inv-dev --timeout=300s

k8s-migrate-prod:
	kubectl delete job gs-inv-migrate -n gs-inv-prod --ignore-not-found=true
	kubectl apply -k k8s/jobs/overlays/prod
	kubectl wait --for=condition=complete job/gs-inv-migrate -n gs-inv-prod --timeout=600s

qa-unit:
	python -m coverage run --source=app -m unittest discover -s tests -p "test_*.py"
	python -m coverage report -m --fail-under=38
	python -m coverage report -m --include="app/repository.py,app/services/*.py,app/auth.py,app/page_common.py,app/config.py" --fail-under=88
	python -m coverage xml -o coverage.xml
	python -m coverage json -o coverage.json

qa-unit-fast:
	python scripts/run_test_suites.py --suite fast --verbosity 1

qa-unit-integration:
	python scripts/run_test_suites.py --suite integration --verbosity 1

qa-e2e:
	PLAYWRIGHT_JSON_OUTPUT_NAME=playwright-results.json npx playwright test --project=chromium --reporter=line,json

qa-evidence: qa-unit qa-e2e
	python scripts/build_qa_evidence.py --coverage-json coverage.json --playwright-json playwright-results.json --output-dir qa-evidence --unit-outcome success --playwright-outcome success --summary-out qa-evidence-summary.md

qa-evidence-build:
	python scripts/build_qa_evidence.py --coverage-json coverage.json --playwright-json playwright-results.json --output-dir qa-evidence --unit-outcome unknown --playwright-outcome unknown --summary-out qa-evidence-summary.md
