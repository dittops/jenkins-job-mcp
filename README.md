# jenkins-mcp

An MCP server that triggers a Jenkins automation job and returns its result.

A single Jenkins job dispatches on its `action_name` build parameter, so the
server exposes a registry of **actions** (`fetchos`, `fetchtop`, `fetchcpu`, …)
rather than a registry of jobs. Adding an action is a JSON edit, not a code
change.

## How a call flows

1. `POST /job/<job>/buildWithParameters?token=…` → Jenkins returns a queue URL
2. Poll the queue item until it is assigned a build number
3. Poll the build until `building == false`
4. Read `consoleText` and extract everything after the `job_output:` marker

Every wait is bounded by a deadline, every response is status-checked, and
polling backs off 2s → 10s.

## Result extraction

Jobs are expected to print a line like:

```
job_output: 17.9.4a
```

The **rest of that line** becomes the tool's `output` field. Capture stops at the
newline: a real console interleaves Jenkins' own epilogue (`Build step ... marked
build as`, `Archiving artifacts`, `[Pipeline] // stage`) between the job's output
and `Finished:`, and consuming past the newline would swallow it. Matching is
case-insensitive and the **last** marker wins, so a rerun reports its final
answer. If no marker is present, `output` is `null` and the response says so,
with `console_tail` for context.

For jobs that print block output (pretty-printed JSON, a table), set
`"output_multiline": true` in the registry. Capture then continues past the first
newline, stopping at a blank line, at a Jenkins-looking line, or at the next
marker.

## Configuration

Copy `.env.example` to `.env` and fill it in — it is read at startup from the
working directory (or `JENKINS_ENV_FILE`). Real environment variables take
precedence, so the `env` block in your MCP client config overrides the file.

| Variable | Purpose |
|---|---|
| `JENKINS_URL` | Base URL, e.g. `http://jenkins.example.com:8080` |
| `JENKINS_USER` | Jenkins user |
| `JENKINS_API_TOKEN` | Jenkins **API token** (prefer over an account password) |
| `JENKINS_BUILD_TOKEN` | Remote-trigger token (`?token=`) |
| `JENKINS_ACTIONS_CONFIG` | Path to `actions.json` |
| `JENKINS_VERIFY_SSL` | Default `true` |
| `JENKINS_TRUST_ENV` | `false` ignores proxy env vars (default) |
| `JENKINS_TIMEOUT_SECONDS` | Hard cap on any wait, default `600` |
| `JENKINS_POLL_INTERVAL` / `JENKINS_POLL_MAX_INTERVAL` | Backoff bounds |
| `JENKINS_CONSOLE_TAIL_LINES` | Console lines returned, default `200` |

### `actions.json`

The registry declares the job, the parameters shared by every action, and the
supported actions. It is also the **allowlist** — an action not listed here
cannot be triggered.

```json
{
  "job": "AUTOMATION_Trigger_Workflow",
  "output_marker": "job_output:",
  "output_multiline": false,
  "parameters": [
    { "name": "customer_name", "required": true, "description": "Customer identifier" },
    { "name": "ipaddress", "required": true, "description": "Device IP address" }
  ],
  "actions": {
    "fetchos": { "description": "Fetch the OS version running on the device." },
    "fetchcpu": "Fetch current CPU utilisation.",
    "fetchtop": { "description": "Top processes", "action_value": "fetch_top_v2" }
  }
}
```

Adding an action is a one-line edit — no code change. Notes:

- An action may declare its own extra `parameters`, merged over the job-wide ones.
- `action_value` sends a different `action_name` than the alias the model sees.
- A bare string is shorthand for `{"description": …}`.

## Tools

| Tool | Purpose |
|---|---|
| `list_jenkins_actions()` | Discover actions and their required parameters |
| `run_jenkins_action(action, parameters, wait=true, timeout_seconds=None)` | Trigger and return the result |
| `get_jenkins_build(build_number=None, queue_url=None, wait=false, timeout_seconds=None)` | Status/result of a build |
| `get_jenkins_console(build_number, tail_lines_count=200)` | Raw console for debugging |

`wait=false` returns as soon as a build number exists, so a long build does not
blow past the MCP client's request timeout; poll `get_jenkins_build` afterwards.
Its wait for a build number is separately bounded (15s) rather than using the
full build deadline — otherwise a backed-up queue would block it for the very
duration the flag exists to avoid. If Jenkins has not assigned a number by then
it returns `status: "QUEUED"` with the `queue_url`, so the triggered build is
never orphaned; pass that URL back to `get_jenkins_build` to resolve it.

Typical success response:

```json
{
  "ok": true,
  "action": "fetchos",
  "build_number": 412,
  "status": "SUCCESS",
  "url": "http://jenkins.example.com:8080/job/AUTOMATION_Trigger_Workflow/412/",
  "duration_ms": 38210,
  "output": "17.9.4a",
  "console_tail": "…"
}
```

Errors are returned as `{"ok": false, "error": …, "error_type": …}` rather than
raised, so the model can read and act on them. Network failures become
`JenkinsConnectionError` with a message naming the host — `httpx.ConnectTimeout`
stringifies to `""`, which would otherwise reach the model as a blank error.

### Troubleshooting

| `error_type` | Meaning |
|---|---|
| `ConfigError` | A required env var is missing, or `actions.json` is absent/malformed |
| `JenkinsConnectionError` | Host unreachable — wrong `JENKINS_URL`, or you are off the VPN |
| `JenkinsAPIError` … `HTTP 401/403` | Bad `JENKINS_API_TOKEN`, or missing/incorrect `JENKINS_BUILD_TOKEN` |
| `JenkinsAPIError` … `HTTP 404` | Job name in `actions.json` does not match Jenkins |
| `UnknownActionError` / `ParameterError` | Rejected before any network call |

Check reachability independently of the server:

```bash
nc -z -G 5 jenkins.example.com 8080 && echo reachable || echo unreachable
```

## Guardrails

- **Action allowlist** — only registry actions run; unknown names are rejected
  before any network call.
- **Parameter validation** — unknown keys are rejected, not forwarded, so extra
  build parameters cannot be smuggled in.
- **`action_name` is server-set** — a caller passing it directly is rejected, so
  the chosen action cannot be spoofed past the allowlist.
- **Credential redaction** — userinfo and query strings are stripped from every
  URL returned or embedded in an error.
- **Console truncation** — default 200 lines, to bound context usage.
- Console output is untrusted device data; the tool descriptions tell the model
  not to follow instructions found inside it.

## Example client

`examples/client_example.py` lists the tools and calls one. It works against
either transport and is the quickest way to check a deployment.

```bash
.venv/bin/python examples/client_example.py
```

That spawns the server over stdio itself — nothing needs to be running first —
and calls the read-only `list_jenkins_actions`. To test a server already
listening over HTTP:

```bash
.venv/bin/python examples/client_example.py --url http://127.0.0.1:8000/mcp
```

To actually start a build, add `--trigger`. This runs a real Jenkins job against
a real device:

```bash
.venv/bin/python examples/client_example.py --trigger --action fetchos \
  --param customer_name=acme --param ipaddress=10.0.0.1 --param hostname=sw1 \
  --param username=admin --param device_type=cisco_ios
```

Add `--no-wait` to exercise the two-step path (return on start, then poll
`get_jenkins_build`). Exit status is 0 only if the build succeeded, so the
script also works as a smoke test in CI.

## Transports

`MCP_TRANSPORT` selects how clients reach the server. The tools are identical
either way.

| Variable | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio`, `streamable-http`, or `sse` |
| `MCP_HOST` | `127.0.0.1` | HTTP bind address |
| `MCP_PORT` | `8000` | HTTP port |
| `MCP_PATH` | `/mcp` | HTTP endpoint path |

### stdio (default, local)

The client spawns the process and owns its stdin/stdout. No port, nothing on the
network. Run by hand and it will sit silently waiting for JSON-RPC on stdin —
that is a healthy server, not a hang. This is the right mode for local Claude
Code; see the `.mcp.json` block below.

### streamable-http (remote URL)

```bash
MCP_TRANSPORT=streamable-http MCP_PORT=8000 .venv/bin/python -m jenkins_mcp.server
```

Requires the `http` extra (`pip install -e ".[http]"`). Serves at
`http://<host>:<port>/mcp`, which is the URL a remote client connects to:

```json
{
  "mcpServers": {
    "jenkins": { "type": "http", "url": "http://your-host:8000/mcp" }
  }
}
```

Config comes from the process environment (or `.env`) rather than a client's
`env` block, since the client no longer launches the process.

### Securing an HTTP deployment

**This server has no authentication of its own.** Anyone who can reach the URL
can trigger Jenkins jobs against your network devices. It binds `127.0.0.1` by
default for that reason.

- Easiest safe remote access: keep the `127.0.0.1` bind and tunnel —
  `ssh -L 8000:127.0.0.1:8000 user@host`.
- If you must bind `0.0.0.0`, put a TLS-terminating authenticating reverse proxy
  in front of it and firewall the port to known clients. Plain HTTP would also
  put the traffic on the wire in cleartext.

## Container image

```bash
docker build -t dittops/jenkins-mcp:0.1.0 .
docker run --rm -p 8000:8000 \
  -e JENKINS_URL=http://jenkins.example.com:8080 \
  -e JENKINS_USER=jenkins-bot \
  -e JENKINS_API_TOKEN=… \
  -e JENKINS_BUILD_TOKEN=… \
  dittops/jenkins-mcp:0.1.0
```

The image defaults to `streamable-http` on `0.0.0.0:8000/mcp` — a container is
reached over the network, and the local `127.0.0.1` default would be
unreachable from outside the container. It runs as uid 10001 and works with a
read-only root filesystem (`--read-only --tmpfs /tmp`). `actions.json` is baked
in at `/app/actions.json`; mount over it, or point `JENKINS_ACTIONS_CONFIG`
elsewhere, to change the registry without rebuilding.

For a stdio client that spawns the container itself:

```bash
docker run -i --rm -e MCP_TRANSPORT=stdio --env-file .env dittops/jenkins-mcp:0.1.0
```

## Kubernetes

`charts/jenkins-mcp` deploys the HTTP transport, with the action registry in a
ConfigMap and the Jenkins credentials in a Secret. The steps below install into
a `jenkins-mcp` namespace.

### 1. Push the image

The cluster has to be able to pull it, so a locally built image is not enough:

```bash
docker build -t dittops/jenkins-mcp:0.1.0 .
docker push dittops/jenkins-mcp:0.1.0
```

For a private registry, add `--set imagePullSecrets[0].name=regcred` at install
time and create that pull secret in the namespace.

### 2. Create the namespace and the credentials Secret

Keep the credentials out of the Helm release. Values passed with `--set` are
stored in the release secret and land in your shell history; a Secret you create
separately does not:

```bash
kubectl create namespace jenkins-mcp

kubectl -n jenkins-mcp create secret generic jenkins-creds \
  --from-literal=JENKINS_USER='jenkins-bot' \
  --from-literal=JENKINS_API_TOKEN='<api-token>' \
  --from-literal=JENKINS_BUILD_TOKEN='<build-token>'
```

Use a Jenkins **API token**, not an account password. Omit
`JENKINS_BUILD_TOKEN` if the job has no remote-trigger token — the chart treats
that key as optional.

The three key names are what the chart reads by default. To reuse a Secret that
names them differently, point the chart at its keys instead of renaming
anything:

```bash
--set jenkins.auth.userKey=username \
--set jenkins.auth.apiTokenKey=api_token \
--set jenkins.auth.buildTokenKey=build_token
```

To rotate a credential later, update the Secret and restart the pods — the
values are read into the environment at startup:

```bash
kubectl -n jenkins-mcp create secret generic jenkins-creds \
  --from-literal=JENKINS_USER='jenkins-bot' \
  --from-literal=JENKINS_API_TOKEN='<new-token>' \
  --from-literal=JENKINS_BUILD_TOKEN='<build-token>' \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n jenkins-mcp rollout restart deploy/jenkins-mcp
```

### 3. Install the chart

```bash
helm upgrade --install jenkins-mcp charts/jenkins-mcp \
  --namespace jenkins-mcp \
  --set jenkins.url=http://jenkins.example.com:8080 \
  --set jenkins.auth.existingSecret=jenkins-creds
```

Add `--dry-run=server` to validate against the live API without installing. The
chart refuses to render without `jenkins.url` and a credential source, so a
half-configured release fails here rather than in CrashLoopBackOff.

Without a pre-made Secret, the chart will render one from values instead:

```bash
--set jenkins.auth.user=jenkins-bot \
--set jenkins.auth.apiToken='<api-token>' \
--set jenkins.auth.buildToken='<build-token>'
```

### 4. Verify

```bash
kubectl -n jenkins-mcp rollout status deploy/jenkins-mcp
kubectl -n jenkins-mcp logs deploy/jenkins-mcp

kubectl -n jenkins-mcp port-forward svc/jenkins-mcp 8000:8000 &
.venv/bin/python examples/client_example.py --url http://127.0.0.1:8000/mcp
```

That lists the tools and calls the read-only `list_jenkins_actions`, which
exercises config loading and the registry without starting a build. Use the
venv's Python: the client needs mcp ≥2.0.0, and a system-wide older mcp fails
with `too many values to unpack`.

In-cluster clients need no port-forward:

```json
{
  "mcpServers": {
    "jenkins": {
      "type": "http",
      "url": "http://jenkins-mcp.jenkins-mcp.svc.cluster.local:8000/mcp"
    }
  }
}
```

### Afterwards

Adding an action is a values edit plus `helm upgrade`, and the pods restart on
the change. Chart values, the `existingSecret` layout, and the scaling and
network caveats are in
[`charts/jenkins-mcp/README.md`](charts/jenkins-mcp/README.md) — the short
version is that the Service stays `ClusterIP`, the Ingress stays off unless
something in front of it authenticates, and `replicaCount` stays at 1 because
streamable-http sessions are held in one pod's memory.

```bash
helm rollback jenkins-mcp -n jenkins-mcp      # previous revision
helm uninstall jenkins-mcp -n jenkins-mcp     # leaves jenkins-creds in place
```

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

Run the tests:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
```

Register with Claude Code (`.mcp.json`), pointing at your real credentials:

```json
{
  "mcpServers": {
    "jenkins": {
      "command": "/path/to/jenkins-mcp/.venv/bin/jenkins-mcp",
      "env": {
        "JENKINS_URL": "http://jenkins.example.com:8080",
        "JENKINS_USER": "jenkins-user",
        "JENKINS_API_TOKEN": "…",
        "JENKINS_BUILD_TOKEN": "your-build-token",
        "JENKINS_ACTIONS_CONFIG": "/path/to/jenkins-mcp/actions.json"
      }
    }
  }
}
```

## Credential note

Use a Jenkins **API token**, not an account password, and keep it in `.env`
(gitignored) or your MCP client's `env` block — never in a tracked file.
