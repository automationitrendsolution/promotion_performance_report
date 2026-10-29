# Live tests

These tests talk to the real Login with Amazon endpoint and the real MySQL
server. They are **deselected by default** (`-m 'not live'` in
`pyproject.toml`) so that `uv run pytest` stays fast, offline and side-effect
free.

Run them deliberately:

```bash
uv run pytest -m live
```

Before you run them, make sure `.env` is populated and points at the intended
database.

These tests are read-only. They exchange the refresh token for an access token
and reflect the table. They never request a report, and they never write,
delete or create anything.
