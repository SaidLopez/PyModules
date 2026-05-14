# Full-stack tracer-bullet demo

End-to-end runnable demonstration of [`pymodules.contrib.fullstack`](../../pymodules/contrib/fullstack/) — the backend half of ADR-0009. One Module, one Command, one Event, a tenant-scoped Outbound policy, an SSE push channel, and a tiny vanilla-JS page that ties it all together.

This is the human-facing equivalent of the integration tests under `tests/contrib/fullstack/`.

## Install and run

From the repo root:

```bash
pip install -e ".[fullstack]"
uvicorn examples.fullstack.app:app --reload --port 8000
```

Then open <http://localhost:8000/> in your browser.

## What it does

- `MessageModule` (`examples/fullstack/app.py`):
  - `published_events = (MessagePosted,)`
  - `@handles(PostMessage)` — accepts a `POST /messages` Command, publishes `MessagePosted` via `host.publish(...)`.
  - `@outbound_policy(MessagePosted)` — returns `event.tenant_id == client.tenant_id`. The SSE endpoint applies this per connected client before pushing.
- The FastAPI app composes:
  - The base API router (`ModuleRouter`) for `POST /messages`.
  - `build_refresh_router(...)` — `POST /__pymodules__/auth/refresh` for rotating cookies.
  - `build_manifest_router(...)` — `GET /__pymodules__/manifest` returning combined OpenAPI + AsyncAPI.
  - `build_sse_router(...)` + `register_with_host(...)` — `GET /__pymodules__/events?subscribe=MessagePosted` and graceful-shutdown drain.
  - `POST /__demo__/login` — **dev shortcut**, see below.
  - `GET /` — serves `index.html`.

## Cross-tenant demo (the point of the slice)

1. Open <http://localhost:8000/> in **tab A**. Log in as user `alice`, tenant `tenant-a`. Status should read `logged in as tenant-a / SSE open`.
2. Open <http://localhost:8000/> in **tab B**. Log in as user `bob`, tenant `tenant-b`.
3. In tab A, set the "Tenant the message belongs to" dropdown to `tenant-a`, type a message, and click **Post**.
4. **Only tab A** receives the message via SSE. Tab B sees nothing.
5. Now post from tab A with the dropdown set to `tenant-b`. **Only tab B** receives it.

Tenant scoping is enforced by the `@outbound_policy(MessagePosted)` method on `MessageModule`. Without that decorator the SSE endpoint would refuse the subscription up-front with a 400 (`no_outbound_policy`) — deny-by-default per ADR-0009.

## Curl-based equivalents

The dev login mints two HttpOnly cookies; save them to a jar so `curl` re-sends them:

```bash
# 1. Log in (mints HttpOnly cookies; saves them to demo.cookies)
curl -X POST -c demo.cookies \
    "http://localhost:8000/__demo__/login?user_id=alice&tenant_id=tenant-a"

# 2. Post a message — the cookie carries the auth
curl -X POST -b demo.cookies \
    -H "Content-Type: application/json" \
    -d '{"tenant_id": "tenant-a", "body": "hello from curl", "posted_by": "alice"}' \
    http://localhost:8000/messages

# 3. Open the SSE stream in a separate terminal (keep the connection alive)
curl -N -b demo.cookies \
    "http://localhost:8000/__pymodules__/events?subscribe=MessagePosted"

# 4. Fetch the manifest (OpenAPI + AsyncAPI in one document)
curl -b demo.cookies http://localhost:8000/__pymodules__/manifest
```

To verify cross-tenant isolation from the shell, run two separate `curl -N` streams with cookies for two different tenants, then `POST /messages` to one and watch only the matching stream receive the SSE frame.

## The `/__demo__/login` shortcut

The PRD ([issue #1](https://github.com/SaidLopez/PyModules/issues/1), "Out of Scope") explicitly scopes the cookie-setting login endpoint out of framework code:

> The login endpoint that **sets** the access and refresh cookies. Application-specific; not framework code.

`/__demo__/login` exists only to make this demo runnable from a fresh clone without standing up an identity provider. It accepts arbitrary `user_id` and `tenant_id` query parameters, signs a JWT, and sets the cookies the framework expects. **Do not copy this into a production application.** A real wiring integrates a proper IdP (OAuth, OIDC, SAML, your auth provider of choice) and derives `tenant_id` from the verified identity, not from a client-supplied string.

## What runs where

| Surface | Route | Provided by |
|---|---|---|
| Command dispatch | `POST /messages` | `ModuleRouter` + `@api_endpoint` |
| Cookie refresh | `POST /__pymodules__/auth/refresh` | `build_refresh_router` |
| Manifest | `GET /__pymodules__/manifest` | `build_manifest_router` |
| SSE push | `GET /__pymodules__/events` | `build_sse_router` |
| Dev login | `POST /__demo__/login` | This example (NOT for production) |
| Static page | `GET /` | This example |

## Production gaps (deliberately left open)

- **Login is faked.** See above.
- **The Command body trusts client-supplied `tenant_id` and `posted_by`.** A real wiring would derive both from `ClientContext` on the authenticated connection. The framework's cookie auth shim already builds a `ClientContext`; production code would inject it (via `Depends(make_cookie_auth_dependency(...))` on the FastAPI route) and overwrite the request fields server-side.
- **Cookies are issued with `Secure=False`** because the demo runs over plain HTTP. In production set `secure=True` on `build_refresh_router` and on `response.set_cookie` in any login wiring.
- **No persistence.** Messages exist only as transient SSE frames. Add a database in production (see `pymodules.contrib.db`).
