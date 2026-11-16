# Try Thallo locally

One command starts the app on this laptop with one fictional bakery, Forno Finto. Nothing is published, sent or spent. The publisher never runs, and no outside service is called.

## Once
- Docker Desktop running, Node.js with npm, and the repo's `.venv`.
- `uvicorn` in the venv: `.venv/bin/python -m pip install 'uvicorn>=0.30,<1'` (listed in the `api` extra).
- The first run calls `npm ci` in `web/`, which needs the network.

## Start
    scripts/demo.sh

1. A throwaway Postgres starts in Docker (container `thallo-demo-pg`, port 54339). The script runs every migration and creates an app role without superuser or row-security bypass.
2. It seeds the demo:
   - five pieces, with copy made through the real copy path by a scripted MOCK writer;
   - images from the real template renderer;
   - check rows from the real writers: three clean, one with a flag, one blocked by a design-lint FAIL;
   - a week plan with five slots;
   - manual connections only.
3. It builds and starts the API and `web/server.mjs`.

Then open http://localhost:3300 in Chrome, Edge or Firefox (tested in Chromium only) and sign in as `demo@thallo.test`. Codes are not emailed; the script prints the command that shows the latest one from the API log.

The script never reads `.env` and ignores any `DATABASE_URL` you have set. Keys (vault, gate signing, proxy stamp, device) are made fresh in a private temporary folder outside the repo, used for this run only, and never printed. Ports can be changed with `DEMO_PG_PORT`, `DEMO_API_PORT` and `DEMO_WEB_PORT`.

## Stop
    scripts/demo.sh stop

This stops both processes, removes the container, and deletes the demo's keys, images and logs.

## Screens
`docs/demo_screens/` holds screenshots of every screen and state at 375 px (`phone-*`) and 1280 px (`desktop-*`).
