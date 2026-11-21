# openoutreach-scout

A read-only LinkedIn scout built on the LinkedIn library of
[OpenOutreach](https://github.com/eracle/OpenOutreach). It runs one LinkedIn account in a
headed Chromium, turns ideal-customer-profile (ICP) search hits and post engagers into
signal records in ClickHouse, and never connects or messages unless both send switches
are on.

- Read-only by default: every upstream connect and message path raises, and the browser
  aborts LinkedIn write requests.
- Human pacing with hard upper bounds. A checkpoint, captcha, login wall or warning page
  stops the scout, alerts, and starts a rest day.
- Image: `ghcr.io/hoolia/openoutreach-scout:<version>`, non-root, runs as any UID in
  group 0 (OpenShift `restricted-v2`).
- Helm chart: `openoutreach` at `https://apps.openshift.eu`.

## Before you run it

LinkedIn's User Agreement prohibits automated access. Running this scout, even read-only
and slowly, can get the account restricted or banned. Read the upstream
[legal notice](https://github.com/eracle/OpenOutreach/blob/ade7fbdbd5a4595581fabf3cec1e803519a299a6/LEGAL_NOTICE.md)
and decide for yourself. The account owner logs in by hand; the scout never types
credentials and never retries a login.

The upstream notice also describes newsletter sign-up and "freemium" promotional
messages. Those live in the upstream Django daemon, which this scout does not ship or
run: the image contains only the `linkedin_cli` package.

## Licence

GPL-3.0-only, see [LICENSE.md](LICENSE.md).

| Part | Licence | Evidence |
| --- | --- | --- |
| `linkedin_cli` at the pinned commit | GPL-3.0 | `LICENCE.md` of eracle/OpenOutreach at `ade7fbd` |
| This scout (`src/oo_scout`) | GPL-3.0-only | it imports `linkedin_cli` into the same process, so it is distributed under the same licence |
| The image | GPL-3.0-only for both parts above | `/licenses` holds both licence texts and the upstream commit; `/opt/upstream` holds the upstream source |

Upstream changed its licence to Apache-2.0 on 2026-11-16 (commit `2a95839`), after
`linkedin_cli` had left the repository. That change does not cover the pinned commit.

## Upstream pin

`upstream.env` pins commit `ade7fbdbd5a4595581fabf3cec1e803519a299a6` of
eracle/OpenOutreach with the SHA-256 of its source archive. Only the Django-free
`linkedin_cli` package and the licence are used; the image carries them in
`/opt/upstream` and `/licenses`.

| Fact | Evidence |
| --- | --- |
| This commit is the last one with `linkedin_cli` in the repository | the next commit, `e74a3aa`, moves it to an external `linkedin-agent-cli` dependency |
| That external dependency no longer resolves | `github.com/eracle/linkedin-cli` and PyPI `linkedin-agent-cli` answer 404 |
| Upstream main has no LinkedIn automation | commit `80cedac` deletes the LinkedIn modules; the README describes a browserless, email-only product |

This repository is not a fork: upstream `main` no longer has the library, and a fork
would carry the daemon this scout leaves out. The pin is one commit and one checksum.

## What it does

| Action | Upstream function | Pacing kind | Signal |
| --- | --- | --- | --- |
| Open the feed at session start | page navigation | page view | none |
| People search per playbook keyword | `actions.search.search_people` | page view | none |
| Read each new profile found | `actions.profile.scrape_profile` (Voyager GET) | page view | `trigger_event`, url and actor_key = profile URL |
| Read a watched post (own or competitor) | page navigation, `browser.nav.extract_in_urls` | page view | `engagement` per profile linked in the post page's `main` column (not the top bar or side rail), url = post URL, actor_key = profile URL |
| Send an approved message | `actions.message.send_raw_message` | message | none |
| Connect with someone not yet connected | `actions.connect.send_connection_request` (no note) | connect | none |

- Rows go to the signal table as JSONEachRow over the ClickHouse HTTP interface, with
  `status=new` and no score. The id is a UUIDv5 of `(source, url, actor_key)`, so repeat
  inserts collapse.
- With `CLICKHOUSE_URL` empty, rows go to `/data/signals.jsonl`.
- ICP search hits use `trigger_event` because the table has no ICP-match type.
- Engagers are the profiles linked on the post page as LinkedIn renders it: the author,
  visible commenters and reaction previews. The full reactions list is not opened.

## Signal table

The scout writes to `CLICKHOUSE_TABLE` (default `signals.signal`). It does not create the
table. A matching definition:

```sql
CREATE TABLE signals.signal (
  id UUID,
  actor_key String,
  source LowCardinality(String),
  url String,
  signal_type Enum8('stated_need' = 1, 'trigger_event' = 2, 'competitor_pain' = 3, 'engagement' = 4, 'visit' = 5),
  actor_person Nullable(String),
  actor_company Nullable(String),
  excerpt String,
  observed_at DateTime,
  rule_id String,
  score Nullable(Float32),
  status Enum8('new' = 1, 'dropped' = 2, 'lead_created' = 3, 'attached' = 4, 'error' = 5),
  odoo_id Nullable(UInt32),
  version UInt64
) ENGINE = ReplacingMergeTree(version)
ORDER BY (source, url, actor_key);
```

- The scout sets only `status = new`; `score`, `odoo_id` and later statuses belong to
  whatever qualifies the signals. Readers query with `FINAL`.
- `excerpt` holds at most 1000 characters.

## Read-only guard

`src/oo_scout/guard.py` installs two layers before any page opens.

1. **Function patches.** Every upstream function in `guard.SEND_PATHS` and the Voyager
   client's `post` raise `ReadOnlyViolation`. `tests/test_guard.py` walks the whole
   upstream package and fails when a function whose name looks like a send is neither
   guarded nor reviewed as a read.
2. **Network block.** A Playwright route aborts non-GET requests to `linkedin.com` that
   touch messaging, invitations, relationships, reactions, comments, follows,
   endorsements or posts, and any Rest.li `?action=` call.

Sending needs `READ_ONLY=false` and `SEND_ENABLED=true`. Only then does the send
contract receive the original functions; the network block is off for that browser.

## Pacing

Each environment value may narrow its bound; a value beyond the bound is a configuration
error and the program exits. The Helm chart's `values.schema.json` holds the same bounds.

| Variable | Default and hard bound |
| --- | --- |
| `SCOUT_WORKING_DAYS` | `mon,tue,wed,thu,fri`; a subset of these |
| `SCOUT_WINDOW_START`, `SCOUT_WINDOW_END` | `08:30`, `18:00` in `SCOUT_TIMEZONE`; not earlier, not later |
| `SCOUT_MAX_PAGE_VIEWS_DAY` | 50 profile, search and post views per day |
| `SCOUT_MAX_CONNECTS_DAY`, `SCOUT_MAX_CONNECTS_WEEK` | 10 per day, 80 per rolling 7 days |
| `SCOUT_MAX_MESSAGES_DAY` | 15 |
| `SCOUT_DELAY_MIN_S`, `SCOUT_DELAY_MAX_S` | random 20 to 120 s between actions; upstream's own short waits are raised to this |
| `SCOUT_SESSION_MAX_MIN` | 45 |
| `SCOUT_PAUSE_MIN_MIN`, `SCOUT_PAUSE_MAX_MIN` | random 30 to 120 min between sessions |
| `SCOUT_REST_HOURS` | 24, at least 24 |

- `SCOUT_TEST_RUN=true` (chart `testRun.enabled`) is an acceptance run: it ignores the
  working window and days, runs one session and then idles. It needs
  `SCOUT_MAX_PAGE_VIEWS_DAY` of at most 5 and `SCOUT_SESSION_MAX_MIN` of at most 10, keeps
  the 20 to 120 s delays, refuses sending, and reads `testRun.playbook` instead of
  `playbook`. Set it back to false and `suspend: true` after the run.
- An action is recorded in `/data/ledger.json` before it runs, so caps survive restarts
  and crashes.
- On a checkpoint, captcha, login wall, warning page or blocked write the scout records
  a halt, posts a Slack alert and stops. It runs again only after `oo-scout resume`
  **and** the rest period.

## Send contract

Off by default. It needs Chatwoot for the human approval and Odoo CRM for the lead. For
each Chatwoot conversation labelled `send-linkedin`:

1. Take the newest private note by a human staff user whose text starts with
   `LinkedIn:`. Notes from users in `CHATWOOT_EXCLUDED_SENDER_IDS` (agent users, bots)
   never count.
2. Read the Odoo `crm.lead` whose id is in the conversation custom attribute
   `CHATWOOT_LEAD_ATTRIBUTE` (default `odoo_lead_id`). If `x_linkedin_sent_note_id` or
   the local ledger already holds the note id, remove the label and stop.
3. Open `x_linkedin_profile`. A first-degree connection gets the text as a message; the
   ledger, then `x_linkedin_sent_note_id` and `x_linkedin_message_sent` on the lead, are
   set and the label is removed. Anyone else gets one connection request without a note
   (upstream has no note path); the label stays and the message follows on a later pass
   after they accept.

- The lead needs three custom fields: `x_linkedin_profile` (char),
  `x_linkedin_sent_note_id` (char) and `x_linkedin_message_sent` (boolean).
- The Chatwoot client's only write is replacing the label list; it cannot post messages
  or notes.

## Configuration

| Variable | Meaning |
| --- | --- |
| `READ_ONLY`, `SEND_ENABLED` | default `true`, `false` |
| `SCOUT_DATA_DIR` | `/data`: browser profile, ledger, dry-run signals |
| `SCOUT_PLAYBOOK` | playbook YAML, `linkedin.rules` (`src/oo_scout/playbook.py`); re-read every session, last valid version kept on error |
| `SCOUT_TIMEZONE` | `Europe/Amsterdam`: time zone of the working window and the day caps, also reported by the browser |
| `SCOUT_LOCALE` | `nl-NL`: browser locale |
| `LINKEDIN_STORAGE_STATE` | optional Playwright storage state that seeds an empty browser profile |
| `CLICKHOUSE_URL`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `CLICKHOUSE_TABLE` | signal store, table default `signals.signal` |
| `SLACK_WEBHOOK_URL` | alerts; without it alerts are logged only |
| `CHATWOOT_URL`, `CHATWOOT_ACCOUNT_ID`, `CHATWOOT_API_TOKEN`, `CHATWOOT_LEAD_ATTRIBUTE`, `CHATWOOT_EXCLUDED_SENDER_IDS` | send contract |
| `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASSWORD` | send contract; the password is an API key |

Commands: `oo-scout run [--once]`, `bootstrap`, `resume`, `status`, `check-config`.

A playbook:

```yaml
linkedin:
  rules:
    - id: icp-platform-engineers       # becomes signal rule_id
      people_keywords: ["platform engineer kubernetes"]
      network: [second, third]         # optional: first, second, third
      max_profiles_per_search: 5       # optional, default 5, at most 10
    - id: engagers-own-posts
      watch_posts:
        - https://www.linkedin.com/feed/update/urn:li:activity:123/
```

## Deploy

Install the Helm chart `openoutreach` from `https://apps.openshift.eu`; its README lists
the values. It renders a Deployment with `replicas: 0` while `suspend: true` (the
default), strategy `Recreate`, a kept PVC for `/data`, the playbook ConfigMap, and no
Service. The pod mounts no service account token. Chromium gets a 1 Gi memory `/dev/shm`.

## Log in once

The account owner logs in by hand in the pod's browser, so the session starts from the
cluster's own egress IP.

1. Set `mode: bootstrap` and `suspend: false`; wait for the pod to run.
2. Forward the VNC port, which listens on the pod's localhost only:
   `oc port-forward deploy/<release> 5900:5900`
3. Open a VNC viewer on `localhost:5900`. The LinkedIn login page is shown. Log in with
   the account's password and second factor.
4. When the feed appears, the log says `Logged in`. The login now lives in the browser
   profile on the PVC.
5. Set `mode: run` and keep `suspend: false`. Check `oc exec deploy/<release> -- oo-scout status`.

Alternative: log in in a local Playwright browser, save its storage state as JSON, and
put it in a Secret with key `storage_state.json` (chart value
`linkedinSession.existingSecret`). The scout adds its LinkedIn cookies to an empty
profile. A login from another IP is more likely to trigger a checkpoint.

After a halt: clear the cause in a bootstrap session if LinkedIn asks for it, then run
`oc exec deploy/<release> -- oo-scout resume`.

## Develop

```sh
sh scripts/fetch-upstream.sh            # linkedin_cli into ./upstream, checksum-verified
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python --require-hashes -r requirements.lock
uv pip install -p .venv/bin/python --no-deps -e .
uv pip install -p .venv/bin/python pytest==9.2.0 respx==0.23.1
.venv/bin/python -m pytest -q
```

`requirements.lock` is generated with
`uv pip compile pyproject.toml --python-version 3.12 --python-platform x86_64-manylinux_2_28 --generate-hashes -o requirements.lock`.

## Release

The workflow `.github/workflows/ci.yaml` runs the tests on every push and pull request.
A tag `v<version>` (the version in `pyproject.toml` and `src/oo_scout/__init__.py`) also
builds the image from `Containerfile` and pushes `ghcr.io/hoolia/openoutreach-scout:<version>`;
the job summary shows the pushed digest. A tag is never pushed twice: an existing image
version fails the job.
