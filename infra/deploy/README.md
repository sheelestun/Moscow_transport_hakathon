# Deploying to the VPS

| Domain | What | How it's served |
|---|---|---|
| `api.mowtransit.ru` | backend: REST, Swagger (`/docs`), WebSocket (`/ws`) | host nginx → `127.0.0.1:8000` |
| `app.mowtransit.ru` | dispatcher dashboard | host nginx → `127.0.0.1:3000` |
| `docs.mowtransit.ru` | Sphinx docs (code + ML + backend) | host nginx, static files |
| `ndtp.mowtransit.ru` | NDTP terminals / the emulator connect here | **no nginx**: raw TCP on port 9201, straight to the backend |
| `mowtransit.ru` | landing page | untouched |

`docker-compose.yml` here runs `ml`, `backend`, `frontend` and `postgres`. The API and the dashboard listen on
127.0.0.1 only, NDTP 9201 is public, ML and Postgres are not published.

Checked locally on 2026-09-27 with the same compose file and copies of these nginx files: every domain through
nginx, the WebSocket upgrade, CORS (the dashboard's origin allowed, others rejected), NDTP on the public port,
and the dashboard in a browser end to end.

## 1. Once: prerequisites

Docker with the compose plugin, nginx (already installed), certbot for nginx, git:

```bash
sudo apt-get install -y certbot python3-certbot-nginx git
docker compose version
```

Firewall — HTTP(S) and NDTP (also open them in the hosting provider's panel if it has its own firewall):

```bash
sudo ufw allow 80,443/tcp && sudo ufw allow 9201/tcp
```

NDTP has no authentication: anyone who can reach 9201 can send telemetry for any unit. If you only feed it from
your laptop, allow just your IP: `sudo ufw allow from <your-ip> to any port 9201 proto tcp`.

## 2. Code, dataset, model

```bash
sudo mkdir -p /srv/mowtransit && sudo chown $USER /srv/mowtransit
git clone <repo-url> /srv/mowtransit/repo && cd /srv/mowtransit/repo
```

- **Dataset**: unpack the organizers' archive into `dataset/` (so `dataset/validate/traffic.csv` exists), or point
  `DATASET_DIR` in `.env` elsewhere. Without it the backend runs but can't match telemetry to vehicles.
- **Model**: put the ML team's artifacts (`catboost_meta.json`, `catboost_seed*.cbm`, `catboost_quantiles.cbm`,
  `catboost_classes.cbm`, `catboost_uncertainty.json`) into `ml/artifacts/`, or set `ML_ARTIFACTS_DIR`. Without
  them the ML container doesn't start and the backend forecasts the baseline (reported in `/health`).

## 3. Configure and start

```bash
cd infra/deploy
cp .env.example .env
$EDITOR .env          # at least POSTGRES_PASSWORD; CLOCK_START for a demo (see below)
docker compose up -d --build
docker compose ps
curl -s 127.0.0.1:8000/health
```

**Demo time.** The dataset is one day, 2026-01-06. By default the dataset clock follows the current Moscow
time of day, so at night the city is empty and after midnight MSK there's no data at all. For a demo set e.g.
`CLOCK_START=2026-01-06T08:00:00` in `.env` and `docker compose up -d backend`.

## 4. nginx and TLS

```bash
sudo cp nginx/*.conf /etc/nginx/sites-available/
for s in api app docs; do sudo ln -sf /etc/nginx/sites-available/$s.mowtransit.ru.conf /etc/nginx/sites-enabled/; done
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d api.mowtransit.ru -d app.mowtransit.ru -d docs.mowtransit.ru --redirect
```

(If this nginx uses `/etc/nginx/conf.d/` instead of `sites-*`, copy the three files there.) certbot rewrites
the files in place to add HTTPS and the redirect, and renews the certificates itself.

## 5. Docs

Build the Sphinx site into the folder `docs.mowtransit.ru` serves:

```bash
sudo mkdir -p /var/www/mowtransit-docs && sudo chown $USER /var/www/mowtransit-docs
cd /srv/mowtransit/repo
docker run --rm -v "$PWD":/repo -v /var/www/mowtransit-docs:/out -w /repo/docs/sphinx python:3.12-slim \
    sh -c "pip install -q sphinx sphinx-rtd-theme && sphinx-build -q -b html . /out"
```

## 6. Check

```bash
curl -s https://api.mowtransit.ru/health          # "status": "ok", ml_available true, history connected
curl -sI https://api.mowtransit.ru/docs           # Swagger UI
curl -sI https://app.mowtransit.ru/               # dashboard
curl -sI https://docs.mowtransit.ru/              # docs
nc -vz ndtp.mowtransit.ru 9201                    # NDTP port reachable
```

Then open https://app.mowtransit.ru — the status in the top right should say «онлайн».

## 7. Live NDTP from a laptop

On the laptop, with the dataset and the emulator image (`docker load -i ndtp-telemetry-emulator.tar`):

```bash
docker run -d --rm -p 18080:18080 --name ndtp-emu ndtp-telemetry-emulator:1.0
python infra/emulator_replay.py --dataset <dataset> --emu-url http://localhost:18080 \
    --target-host ndtp.mowtransit.ru --target-port 9201 --clock-url https://api.mowtransit.ru
```

`--clock-url` makes the replayed tracks follow the server's dataset clock. `GET https://api.mowtransit.ru/ingest/vehicles`
shows which vehicles are live on NDTP; when the laptop stops, they fall back to the replay after 60 s.

## Operations

```bash
docker compose logs -f backend                            # logs
git pull && docker compose up -d --build backend          # update the backend
docker compose exec postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup.sql   # backup
```

Load test on the VPS (NDTP ingest capacity, numbers for the submission — see `backend/README.md`), from a venv
on the host; it starts its own server on a free local port:

```bash
python3 -m venv ~/lt && ~/lt/bin/pip install -q -r /srv/mowtransit/repo/backend/requirements.txt
~/lt/bin/python /srv/mowtransit/repo/backend/scripts/ndtp_loadtest.py --units 8000 --loop uvloop
sysctl net.core.somaxconn    # should be >= 4096
```
