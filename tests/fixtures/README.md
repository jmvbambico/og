# Test fixtures

## `herdr_api_schema.json`

herdr's own published JSON schema, captured verbatim from
`herdr api schema --json`. Committed rather than fetched at test time because CI
has no herdr, and because a conformance test that silently degrades to "no
schema found, assume the client is right" is worse than no test at all.

### Recorded from

| | |
|---|---|
| `protocol` | 22 |
| `schema_version` | 1 |
| size | 272 KB |
| captured | 2026-10-10 |

`protocol` is the number to check first when a test fails against a newer
herdr: if the installed server reports a different protocol, regenerate before
concluding that the client is wrong.

### Regenerate

```sh
herdr api schema --json > tests/fixtures/herdr_api_schema.json
```

`herdr api schema` is read-only metadata and talks to no session — safe to run
against a live herdr with real panes in it (this is the only herdr command the
tests' own safety rules permit). After regenerating, update the table above.

Then re-read the diff. A regeneration that changes a params schema this client
depends on is the whole point of vendoring the file: it is how a herdr upgrade
reaches CI instead of production.

### What it is used for

`tests/test_og_herdr_client.py` validates every request frame the client can
emit against `schemas.request.oneOf`: the method name must be a real variant,
every param key sent must exist in that variant's params schema, and every
required param must be one the client actually sends.

This exists because four shipped defects shared one cause — the client's idea of
the wire contract was never checked against herdr's:

| defect | what the client sent | what the schema says |
|---|---|---|
| `pane.run` is not a method | `{"method": "pane.run"}` | no such variant; it is a CLI subcommand |
| `tab.create` workspace | `{"workspace": …}` | `workspace_id`, and unknown keys are dropped |
| `pane.read` payload | looked for `text`/`output`/`content`/`data`/`lines` | `{"type": …, "read": {"text": …}}` |
| `events.subscribe` | `types=` | `subscriptions` |

None of them were visible to the test fake, which replied to *any* method name
with an empty object. The fake is now strict for the same reason.