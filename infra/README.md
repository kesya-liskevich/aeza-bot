To apply the schema quickly (MVP way):

docker compose cp infra/alembic/versions/0001_init.sql postgres:/0001_init.sql
docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB -f /0001_init.sql
