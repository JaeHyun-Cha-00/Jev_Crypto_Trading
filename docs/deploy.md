# Deploying

## Live website (free, on Render)

`render.yaml` deploys the dashboard as one free Render web service at
`https://<name>.onrender.com`, so it works from a phone with your computer off.
`Dockerfile.hosted` builds the dashboard into the API image, and the API serves
both (`jevtrade.api.hosting`). It costs nothing: Render's free plan gives 750
instance hours a month, enough for one service around the clock, and needs no card.

Because the page is on the public internet, it asks for `DASHBOARD_PASSWORD`
(any user name). Only `/api/health` is open, for Render's health check. The
same service runs the collect backstop (`JEVTRADE_KICK=1`, at :25, after the
local `collect-kick` at :20, so both can run) and pings itself every 10
minutes, because free services otherwise sleep after 15 idle minutes.

1. Sign in at https://dashboard.render.com with GitHub and let Render see this repo.
2. New → Blueprint → pick this repo. Render reads `render.yaml`.
3. Fill in `DASHBOARD_PASSWORD` (pick one) and `GITHUB_TOKEN` (the one in your
   `.env`: Contents read, Actions read and write), then Apply.
4. When the deploy is live, open the `onrender.com` URL shown on the service.

Every merge to `main` that touches the app redeploys. The free plan includes
500 build minutes a month; without a card on file Render pauses builds (never
bills) if they run out, and the last deploy keeps serving. If the service ever does sleep, the first visit
takes about a minute to wake it. Nothing hosted trades or holds exchange keys.

## Running on a fresh Linux VM

`docker compose` runs two services. The dashboard shows Jev's hourly forward
log, which the collect workflow (GitHub Actions) writes to the `data-log`
branch, so the VM itself never calls Jev, trades, or stores candles.

| Service | What it does |
|---|---|
| `api` | `python -m jevtrade.api`: read-only API that reads the forward log from GitHub, reachable only from `web` |
| `web` | nginx serving the dashboard and passing `GET /api/*` to `api`; published on `127.0.0.1:8080` |

The local paper loop (`python -m jevtrade.paper run`, section 7) is not part
of the compose stack; run it yourself if you want it.

A 1 vCPU / 1 GB VM (any provider, Ubuntu 24.04 or Debian 12) is enough. The
VM needs outbound HTTPS only. Open no inbound ports other than SSH.

1. **Install Docker** (as a user with sudo):

   ```bash
   sudo apt-get update && sudo apt-get install -y git ca-certificates curl
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER && newgrp docker
   ```

2. **Get the code and configure it:**

   ```bash
   git clone https://github.com/JaeHyun-Cha-00/Jev_Crypto_Trading.git
   cd Jev_Crypto_Trading
   cp .env.example .env    # set GITHUB_TOKEN only if the repo is private
   ```

3. **Start everything:**

   ```bash
   docker compose up -d --build
   docker compose ps                  # api should become "healthy"
   docker compose logs -f api
   ```

   `restart: unless-stopped` brings both back after a crash or a reboot.

4. **Open the dashboard from your laptop** through an SSH tunnel (the port
   is bound to the VM's localhost and has no login):

   ```bash
   ssh -N -L 8080:localhost:8080 you@your-vm
   # then browse to http://localhost:8080
   ```

Day to day:

```bash
docker compose restart api          # after editing config/default.yaml (mounted read-only)
git pull && docker compose up -d --build --remove-orphans   # update
docker compose down                 # stop
```

Upgrading from a version that also ran a `paper` service: `--remove-orphans`
stops its old container. Its data stays in the `jevdata` and `jevreports`
volumes until you delete them (`docker volume ls`, then `docker volume rm`).
