# Jawafdehi cross-service integration tests (against the unified monolith).
#
# `up`/`down` drive the monolith docker-compose stack. The compose file lives at
# the repo root (one monolith image on a single host:port — default :48000, with
# Zitadel :48080 and OpenSearch :49200). `test` targets assume the platform base
# URL / OIDC creds come from the environment (see .env / .env.example).

COMPOSE_DIR ?= ..
PYTEST ?= pytest

.PHONY: help test test-smoke test-contract up down logs

help:
	@echo "Targets:"
	@echo "  make up           # bring up the monolith compose stack (detached)"
	@echo "  make down         # tear the stack down"
	@echo "  make test         # run the full suite (needs the monolith up)"
	@echo "  make test-smoke   # run only the fast per-prefix smoke checks"
	@echo "  make test-contract# run pure-contract tests only (no stack needed)"
	@echo "  make logs         # tail the compose stack logs"

# Full suite. SKIP_IF_STACK_DOWN=1 turns 'stack down' into skips, not failures.
test:
	$(PYTEST)

# Fast per-prefix contract checks (NES/NGM/Jawafdehi health + basic shapes).
test-smoke:
	$(PYTEST) -m smoke

# Pure-contract tests that don't touch the stack (safe with no stack).
test-contract:
	$(PYTEST) -m "not live" -p no:cacheprovider

# Bring up / tear down the monolith stack (compose file at the repo root).
up:
	cd $(COMPOSE_DIR) && docker compose up -d

down:
	cd $(COMPOSE_DIR) && docker compose down

logs:
	cd $(COMPOSE_DIR) && docker compose logs -f
