SHELL := /bin/bash
include .env

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

migrate:
	docker compose cp infra/alembic/versions/0001_init.sql postgres:/0001_init.sql || true
	docker compose exec postgres psql -U $$POSTGRES_USER -d $$POSTGRES_DB -f /0001_init.sql

seed:
	docker compose exec postgres psql -U $$POSTGRES_USER -d $$POSTGRES_DB -c "INSERT INTO managers (tg_id, name, role) VALUES ('123','Менеджер 1','manager') ON CONFLICT DO NOTHING;"
