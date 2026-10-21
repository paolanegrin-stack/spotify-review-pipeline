# Dashboard setup

The static dashboard uses plain JavaScript and CSS charts. `api/index.py` is a
Vercel Python function dispatching the five read-only API routes. Nothing calls
a model. The loader and preview make no connections at import time.

1. Create a Neon Postgres project/database in the Neon console if you do not
   already have one. Put its connection string in `.env` as `DATABASE_URL`.
   Use the existing database when already configured. Never commit the URL.
2. Install the driver with ` .\.venv\Scripts\python -m pip install -r api/requirements.txt`.
   The root requirements file includes this file so Vercel installs the driver too.
3. Run ` .\.venv\Scripts\python -m src.load_db --schema-only` to execute
   `db/schema.sql`. Prefer a direct Neon connection for schema administration.
4. Wait for `full_v1` to finish and for its saved issues, ranking, claims, memo
   and summary to exist. Run the separately authorized out-of-scope quarantine
   command if you want the excluded IDs shown as quarantined instead of pending.
   Then run ` .\.venv\Scripts\python -m src.load_db --run-id full_v1`.
   This reads the SQLite database in read-only mode, streams all source IDs via
   COPY, and replaces that run in one PostgreSQL transaction. Counts print after
   commit. No files under `runs/` are written. COPY avoids per-record network trips;
   actual load duration depends on Neon compute and the connection.
5. Deploy the repository to Vercel with Framework Preset **Other**, repository
   root as Root Directory, and no custom build command/output override. The
   checked-in `vercel.json` builds the static file and Python function and routes
   `/` and `/api/*`. Set `DATABASE_URL` in Vercel's environment settings, normally
   using Neon's pooled connection string, then deploy/redeploy. Vercel uses the
   environment variable; the browser never receives the connection string.
   `.vercelignore` excludes `.env`, raw datasets, local runs, and the virtual environment.

For local preview after loading, run
` .\.venv\Scripts\python -m src.preview_dashboard --port 8000` and open
`http://127.0.0.1:8000/`. This uses `.env`, serves the same API handler, and queries
Neon when the browser loads. Stop this preview yourself with Ctrl+C. A static-only
`http.server` preview cannot serve the API.

API routes default to `full_v1`: `/api/overview`, `/api/ranking`,
`/api/issues/ISS-PLAYBACK`, `/api/memo`, `/api/claims`. All values are SQL
parameters; every request runs in a read-only database transaction. The endpoints
are public dashboard reads. Memo rendering treats HTML as text, supports headings,
lists, bold and code, and makes claim references clickable. Check failures remain
visible rather than presenting the memo as validated.

`records.review_id` is the primary key as requested. Reloading the same run is
supported; loading another run with overlapping review IDs fails and rolls back
instead of overwriting that other run's records. Source rating/timestamp/version
values remain text. The loader derives verifier agreement from saved sample IDs
and predictions, matched to source hashes and the saved enrichment configuration.
The scope count comes from saved `scope_ids.txt`; this dashboard declares 100,000
nonempty reviews. The difference from the checked-in grading contract is documented
in `docs/scope.md`.
