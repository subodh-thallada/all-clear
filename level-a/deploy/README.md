# Deploying the API

Only the API needs a public HTTPS URL: ElevenLabs calls its tools
(`/tools/submit_count`, `/tools/submit_answer`, `/tools/request_callback`) and
its post-call webhook (`/hooks/post_call`) during real calls, the judge may open
the party form on a phone, and the wall is served from it. Host it somewhere
that does not depend on the venue's Wi-Fi.

| Process | Where | Why |
|---|---|---|
| API (`python -m levela api`) | Render, Fly.io or Railway | Public URL for the webhooks; hosts the simulator's calls and the scheduler (invariants once per simulated hour, scheduled order system changes) |
| Worker under the supervisor (`python scripts/supervisor.py`) | Team laptop | It is killed on purpose; the restarts are part of the demo |
| Mock order systems (`scripts/start_order_systems.sh`) | Team laptop | State in memory, one process per distributor, own faults and version switch |
| Atlas | The sandbox cluster | System of record for everything above |

`API_BASE_URL` means "how this process reaches the API": on the host it is
`http://127.0.0.1:8000` (the API's simulator posts its replies back to the API),
on the laptop it is the host's public URL. `PUBLIC_BASE_URL` is the public URL
everywhere. Secrets go in the host's dashboard (or `fly secrets set`), never in
a file in git. Use the same `TOOL_SECRET`, `ADMIN_TOKEN` and
`ELEVEN_WEBHOOK_SECRET` on the host and in the laptop's `.env`.

## Render (the default)

1. Render dashboard: New > Blueprint, pick this repository, and set the
   Blueprint file path to `level-a/deploy/render.yaml`.
2. Fill every value Render asks for (the `sync: false` ones): `ATLAS_URI`, the
   three shared secrets, the ElevenLabs and Twilio keys and ids, the two phone
   numbers, `EMBED_API_KEY` (or leave it empty for the hash embedder),
   `ANTHROPIC_API_KEY` (or empty: rules decider and rules repairer),
   `ORDER_NORTH_URL` and `ORDER_EAST_URL` (below), and `PUBLIC_BASE_URL`
   (leave empty for the first deploy).
3. After the first deploy, copy the service URL
   (`https://levela-api-xxxx.onrender.com`) into `PUBLIC_BASE_URL` on Render and
   into `PUBLIC_BASE_URL` and `API_BASE_URL` in the laptop's `.env`, then deploy
   once more from the dashboard (auto-deploy is off on purpose: a deploy restarts
   the API, its in-flight simulated calls and its scheduler).
4. Check `https://<host>/healthz` answers `{"ok": true, ...}`.

The free plan sleeps after 15 minutes without inbound traffic and takes about a
minute to wake; while it sleeps the scheduler does not run. Open `/healthz`
ten minutes before the show and keep the wall open, or switch the service to
the starter plan for the judged run.

## Fly.io

From `level-a/`:

```sh
fly launch --no-deploy --copy-config --config deploy/fly.toml --name <unique-name>
fly secrets set --config deploy/fly.toml ATLAS_URI='...' TOOL_SECRET='...' ADMIN_TOKEN='...' \
    ELEVEN_WEBHOOK_SECRET='...' ELEVEN_API_KEY='...' ELEVEN_AGENT_ID='...' ELEVEN_PHONE_ID='...' \
    TWILIO_SID='...' TWILIO_TOKEN='...' PHONE_HARBOUR='...' PHONE_CORNER='...' EMBED_API_KEY='...' \
    ANTHROPIC_API_KEY='...' ORDER_NORTH_URL='...' ORDER_EAST_URL='...' \
    PUBLIC_BASE_URL='https://<unique-name>.fly.dev'
fly deploy . --config deploy/fly.toml --dockerfile deploy/Dockerfile --ha=false
fly scale count 1 --config deploy/fly.toml
```

`--ha=false` and `scale count 1` keep exactly one machine: a second one would run
a second scheduler. `fly.toml` keeps it always on (`auto_stop_machines = "off"`).

## Railway

1. New Project > Deploy from GitHub repo. In the service's settings set Root
   Directory to `level-a`, and add the variable `RAILWAY_DOCKERFILE_PATH` =
   `deploy/Dockerfile` so it builds the same image as Fly.
2. Variables: everything in `deploy/render.yaml` (values and secrets), with
   `PORT=8000` and `API_BASE_URL=http://127.0.0.1:8000`.
3. Settings > Networking > Generate Domain; put it in `PUBLIC_BASE_URL`.
   Settings > Deploy > Healthcheck Path: `/healthz`. Keep one replica.

## Order systems behind a tunnel

The hosted API proxies the operator's "Switch order system to v2" button to the
order systems and reads their hold logs for invariant 2, so it must reach the
laptop's order systems. Expose each with a Cloudflare quick tunnel (no account
needed; `cloudflared` from Cloudflare's downloads page):

```sh
scripts/start_order_systems.sh                                # north on 8101, east on 8102
cloudflared tunnel --url http://127.0.0.1:8101          # prints https://<words>.trycloudflare.com
cloudflared tunnel --url http://127.0.0.1:8102
```

Set `ORDER_NORTH_URL` and `ORDER_EAST_URL` on the host to the two printed
URLs. The laptop's `.env` keeps `http://127.0.0.1:8101` and `:8102` (the worker
reaches the order systems directly). Quick-tunnel URLs change on every start: update
the host after restarting a tunnel. If the API cannot reach an order system, the
invariant check reports that order system's log as unknown rather than passing it,
and the switch button answers 502.

## Backup: the API on the laptop behind a tunnel

If the host is down, run everything on the laptop and expose only the API:

```sh
python -m levela api --host 127.0.0.1 --port 8000
cloudflared tunnel --url http://127.0.0.1:8000         # or: ngrok http 8000
```

Put the printed `https://` URL in `PUBLIC_BASE_URL`, set
`API_BASE_URL=http://127.0.0.1:8000` and the order system URLs back to
`http://127.0.0.1:8101` / `:8102` in `.env`, and restart the API. ngrok needs
an account and its authtoken (`ngrok config add-authtoken ...`) once; a free
ngrok URL also changes on every start.

## Every time the public URL changes

The ElevenLabs tools and the post-call webhook point at `PUBLIC_BASE_URL`, so
after any new URL (a new host, a new tunnel):

```sh
python scripts/provision_agent.py --dry-run     # check the URLs it will set
python scripts/provision_agent.py               # repoints the three tools and the agent
```

If it creates a new post-call webhook (it warns when `ELEVEN_WEBHOOK_ID`
points at the old URL), it prints the new signing secret once: put it in
`ELEVEN_WEBHOOK_SECRET` on the host and in `.env`, and set the printed
`ELEVEN_WEBHOOK_ID`.

## Check it from a phone on mobile data

Turn Wi-Fi off on a phone and open, on the public URL:

1. `/healthz`: `{"ok": true, ...}` (the API reaches Atlas);
2. `/wall`: the wall loads and updates once a second;
3. a party form link printed by `make demo` (`/form/<token>`): the form loads.

Then place the smoke call (`python scripts/call_once.py --watch`) and check the
`tool_call` row lands in `tool_events`: that proves ElevenLabs reaches the host.
