# Working in a Foundation-based project

- Project code goes in `app/` only. Do not edit `foundation/` inside a project; Foundation changes are made in galinskylaboratories/Foundation and merged into projects.
- New tables: define them in `app/models.py` on `foundation.db.Base`, then `python scripts/manage.py makemigration "message"`. New columns on existing tables need a `server_default`.
- Pages and API routes: `app/routes.py` exposes `router`. Use `render(request, "name.html", ctx)` from `foundation.web`; templates in `app/templates` extend `base.html`.
- Access control: `Depends(require_user)`, `Depends(require_role("staff"))`, and for JSON APIs `Depends(require_api_key("scope"))`.
- Every HTML form includes `{{ csrf_field(request) }}`. No inline `<script>`; put JavaScript in `app/static/`.
- Files: `foundation.blobstore` (never rely on the local disk; Render erases it). Background work: `foundation.jobs`. Email: `foundation.mail.send_email`. AI: `foundation.llm`. Fetching user-supplied URLs: `foundation.net.safe_get`. Calling another service: `foundation.client.ServiceClient`.
- Record significant actions with `foundation.db.audit(session, "thing.action", user.id, target=...)`.
- Tests: `python -m pytest -q`; fixtures in `tests/conftest.py` (`client`, `db`, `login`, `csrf`). CI runs them on SQLite and Postgres.
