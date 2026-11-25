# Mealie gate (design, not implemented)

Status: documented on 2026-11-25, deliberately **not built**. The user chose to keep things simple for now. Build it with the container and network hardening of phase 4 (`docs/superpowers/specs/2026-11-23-structured-interface-and-hardening-design.md`) if the extra protection becomes worth one more container.

## Why

Mealie API tokens carry all the rights of the user that created them, and Mealie has no read-only role. Two tokens, or two users, therefore differ only in name and in being revocable separately. Today the read/write separation is enforced by the application: `MealieReader` only sends `GET`, and only the Confirmer commit uses `MealieClient`. That stops the model and the tools from writing, but a compromised container still holds a token that can do anything the Mealie user can do (delete recipes, edit other settings).

## Design

A tiny reverse proxy (nginx or Caddy) sits between the bot and Mealie and enforces the separation outside the bot.

```text
agents container ──(no Mealie token)──► gate :read   ─ GET only ────────────┐
                 ──(no Mealie token)──► gate :write  ─ allowlist below ─────┼─► Mealie
                                        gate adds `Authorization: Bearer <token>`  (nobody else can reach Mealie)
```

- The gate holds the Mealie token(s). The agents container holds none. With the gate, one Mealie token is enough and the two-token split becomes optional.
- The read listener forwards `GET` only.
- The write listener forwards only what the Confirmer commit uses (taken from `app/mealie.py`), and nothing else, so no `DELETE`, no users, no admin or household settings:

| Method | Path |
|---|---|
| `POST` | `/api/recipes` |
| `PATCH` | `/api/recipes/{slug}` |
| `POST` | `/api/organizers/tags` |
| `POST` | `/api/households/mealplans` |
| `POST` | `/api/households/shopping/lists` |
| `POST` | `/api/households/shopping/items/create-bulk` |

- Mealie must accept connections only from the gate (a firewall rule on the NAS, or a Docker network shared only by the gate and Mealie). Otherwise anything on the LAN that learns a token can still reach Mealie directly.

## What it would change in the app

- A URL per role (for example `MEALIE_READ_URL`, `MEALIE_WRITE_URL`) instead of one `MEALIE_URL`, and the tokens become optional in the app.
- `MealieReader` and `MealieClient` keep their method restrictions as a second line of defence.
- The allowlist above must be updated whenever `MealieClient` gains a call; a test that compares the two would keep them in step.

## What it costs

One more container and about 30 lines of proxy configuration, plus the Mealie-side network restriction. The proxy rules need to be tested against the real instance (the write endpoints in `docs/mealie-endpoints.md` are only verified through the Confirmer live test).
