# INIM Prime HA integration — Docker test pipeline
# Panel creds live in .env (gitignored). HA version is switchable via HA_VERSION / HA_IMAGE_VERSION.

.PHONY: build unit ha-unit e2e e2e-arm test ha-up ha-down ha-logs clean

build:                ## build the test image
	docker compose build

unit: build           ## pure client unit tests (tests/)
	docker compose run --rm unit

ha-unit: build        ## HA integration unit tests (tests_ha/)
	docker compose run --rm ha-unit

e2e: build            ## live end-to-end against the real panel (read-only)
	docker compose run --rm e2e

e2e-arm: build        ## live E2E incl. Box arm/disarm roundtrip
	docker compose run --rm -e INIM_E2E_ARM=1 e2e

test: unit ha-unit    ## all offline tests

ha-up:                ## start a real HA server with the integration mounted (http://localhost:8123)
	docker compose up -d homeassistant
	@echo "Home Assistant at http://localhost:8123 — add the 'INIM Prime' integration via Settings > Devices."

ha-down:
	docker compose down

ha-logs:
	docker compose logs -f homeassistant

clean:
	docker compose down -v
