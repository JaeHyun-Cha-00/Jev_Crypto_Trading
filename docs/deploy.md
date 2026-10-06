# Deploying

## Live website (free, on Render)

`render.yaml` deploys the dashboard as one free Render web service at
`https://<name>.onrender.com`, so it works from a phone with your computer off.
`Dockerfile.hosted` builds the dashboard into the API image, and the API serves
both (`jevtrade.api.hosting`). It costs nothing: Render's free plan gives 750
instance hours a month, enough for one service around the clock, and needs no card.

Because the page is on the public internet, it asks for `DASHBOARD_PASSWORD`
(any user name). Only `/api/health` is open, for Render's health check. The
same service runs the collect backstop (`JEVTRADE_KICK=1`, from :03 past each
hour) and pings itself every 10 minutes, because free services otherwise sleep
after 15 idle minutes.

1. Sign in at https://dashboard.render.com with GitHub and let Render see this repo.
2. New → Blueprint → pick this repo. Render reads `render.yaml`.
3. Fill in `DASHBOARD_PASSWORD` (pick one) and `GITHUB_TOKEN` (the one in your
   `.env`: Contents read, Actions read and write), then Apply.
4. When the deploy is live, open the `onrender.com` URL shown on the service.

Every merge to `main` that touches the app redeploys. The free plan includes
500 build minutes a month; without a card on file Render pauses builds (never
bills) if they run out, and the last deploy keeps serving. If the service ever does sleep, the first visit
takes about a minute to wake it. Nothing hosted trades or holds exchange keys.
