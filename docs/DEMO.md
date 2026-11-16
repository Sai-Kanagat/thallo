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

## Sai's own brand
    scripts/demo.sh --brand sai

Same app and the same safety (local Docker database, temporary keys, no `.env`, nothing sent), with Sai Kanagat's brand instead of the bakery:
- his brand tokens and Bricolage Grotesque files from `demo_brands/sai/`, rendered by the real template renderer;
- the 32 claims in `demo_brands/sai/claims.json`, each with its page as source. Twenty-six are verified. The six the site contradicts itself on (C04, C07, C09, C13, C14, C18; see `GAPS.md`) are stored as candidates, so no piece can use them;
- his voice rules and retired words from `voice.md`;
- five LinkedIn pieces written by the scripted mock writer: a project post, a case-study card, a quote card, a stat card with a flag to read, and a draft that typed numbers instead of using the claim, which stays blocked by a FAIL.

The brand values that are proposals, not facts from his site, are in `demo_brands/sai/PROPOSED.md`. Renders to judge them by eye are in `docs/demo_screens/sai/`; `python scripts/demo_sai_renders.py` redraws the family samples.

A demo already running (for example the bakery) blocks a second one on the same names. To run both, give the second its own folder, container and ports, and use the same `DEMO_STATE` and `DEMO_CONTAINER` with `stop`:

    DEMO_STATE="$TMPDIR/thallo-demo-sai" DEMO_CONTAINER=thallo-demo-sai-pg DEMO_PG_PORT=54349 \
      DEMO_API_PORT=8775 DEMO_WEB_PORT=3310 scripts/demo.sh --brand sai

## Stop
    scripts/demo.sh stop

This stops both processes, removes the container, and deletes the demo's keys, images and logs.

## Screens
`docs/demo_screens/` holds screenshots of every screen and state at 375 px (`phone-*`) and 1280 px (`desktop-*`).
