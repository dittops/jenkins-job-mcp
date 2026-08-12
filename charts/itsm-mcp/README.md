# itsm-mcp chart

Deploys the ITSM MCP server as a `streamable-http` endpoint that in-cluster MCP
clients connect to at
`http://<release>-itsm-mcp.<namespace>.svc.cluster.local:8000/mcp`.

The image is `dittops/itsm-mcp`, built from
[`Dockerfile.itsm`](../../Dockerfile.itsm) at the repo root:

```bash
docker build -f Dockerfile.itsm -t dittops/itsm-mcp:0.1.0 .
docker push dittops/itsm-mcp:0.1.0
```

It shares the source tree and the wheel with `dittops/jenkins-mcp` — the
project installs both entry points — but is its own image with its own
`ENTRYPOINT` and its own baked-in config, so the two deployments version and
roll back independently.

## Install

```bash
helm upgrade --install itsm-mcp charts/itsm-mcp \
  --namespace itsm-mcp --create-namespace \
  --set itsm.url=https://10.10.146.120 \
  --set itsm.auth.authtoken=<authtoken>
```

Better, once a Secret is managed out of band — nothing sensitive then passes
through `--set` (where it lands in shell history and the Helm release):

```bash
kubectl -n itsm-mcp create secret generic itsm-creds \
  --from-literal=ITSM_AUTHTOKEN=…

helm upgrade --install itsm-mcp charts/itsm-mcp -n itsm-mcp \
  --set itsm.url=https://10.10.146.120 \
  --set itsm.auth.existingSecret=itsm-creds
```

The chart refuses to render without `itsm.url` and a credential source, so a
half-configured release fails at `helm template` rather than in CrashLoopBackOff.

## Values

| Key | Default | Purpose |
|---|---|---|
| `image.repository` / `image.tag` | `dittops/itsm-mcp` / chart appVersion | Image to run |
| `image.command` | `[]` | Empty uses the image's `ENTRYPOINT`. Only set it to run a different entry point out of the same image |
| `replicaCount` | `1` | See [Scaling](#scaling) before raising |
| `itsm.url` | — | **Required.** ITSM base URL |
| `itsm.portalId` | `"1"` | Sent as the `PORTALID` header |
| `itsm.auth.existingSecret` | `""` | Secret carrying the authtoken; when empty the chart renders one from `itsm.auth.authtoken` |
| `itsm.auth.authtokenKey` | `ITSM_AUTHTOKEN` | Key name inside that Secret |
| `itsm.verifySsl`, `itsm.timeoutSeconds`, `itsm.trustEnv` | `true` / `30` / `false` | Client behaviour |
| `policy` | one group, notes internal-only | Rendered to `itsm.json` in a ConfigMap |
| `existingPolicyConfigMap` | `""` | Use your own ConfigMap (key `itsm.json`) instead |
| `mcp.transport` / `mcp.port` / `mcp.path` | `streamable-http` / `8000` / `/mcp` | Transport wiring; `stdio` is rejected — there would be no port to serve |
| `ingress.enabled` | `false` | Read [Security](#security) first |
| `networkPolicy.enabled` / `.from` | `false` / `[]` | Restrict who may connect |
| `extraEnv`, `extraEnvFrom`, `extraVolumes`, `extraVolumeMounts` | `[]` | Escape hatches, e.g. a private CA bundle |

## The write policy

`policy` lives in `values.yaml` and is rendered into a ConfigMap, so changing
what the server may write is a values edit and an upgrade — no image rebuild:

```yaml
policy:
  groups:
    - name: ICCM Tools
      description: ICCM tooling and automation queue.
    - name: Network Ops
  notes:
    max_length: 5000
    defaults:
      show_to_requester: false
      mark_first_response: false
      add_to_linked_requests: false
    allowed:
      show_to_requester: true
      add_to_linked_requests: true
```

`groups` also accepts the shorthand `["ICCM Tools", "Network Ops"]`. The
deployment carries a checksum annotation over the rendered JSON, so
`helm upgrade` restarts the pods when the policy changes.

Two things this list is doing:

- **`groups` is the allowlist.** A group not listed here cannot be assigned. An
  empty list means *unrestricted*, which the chart warns about on install.
- **`allowed` is a hard gate**, `defaults` only sets what applies when the
  caller says nothing. `show_to_requester` publishes a note to the customer and
  `add_to_linked_requests` copies it onto every linked ticket — set either to
  `false` under `allowed` and no caller can turn it on.

## Security

The server has **no authentication**, and unlike the Jenkins one it *writes*:
anyone who can reach the URL can reassign tickets and post notes under the
authtoken's technician account.

- Scope the authtoken to a technician who may only reassign groups and add
  notes. The server's own policy bounds which fields it will write; the token's
  permissions are what bounds a compromised pod.
- The Service is `ClusterIP` and the Ingress is off by default. Keep it that way
  unless whatever you put in front terminates TLS *and* authenticates callers.
- Turn on `networkPolicy.enabled` and list the client pods in
  `networkPolicy.from`. With `from` empty the policy denies all ingress, which
  is a safe default but will also cut off your own clients.
- The pod runs as uid 10001, non-root, all capabilities dropped, with a
  read-only root filesystem and no service account token mounted.

An instance addressed by IP normally presents a cert that fails hostname
verification. Mount your CA and point `SSL_CERT_FILE` at it rather than setting
`itsm.verifySsl=false` — the authtoken is a bearer credential on every request:

```yaml
extraVolumes:
  - name: ca
    secret:
      secretName: itsm-ca
extraVolumeMounts:
  - name: ca
    mountPath: /certs
    readOnly: true
extraEnv:
  - name: SSL_CERT_FILE
    value: /certs/ca.pem
```

## Scaling

Leave `replicaCount` at 1. A streamable-http session lives in the memory of the
pod that created it, so a request routed to a different replica fails with an
unknown-session error. If you must run more than one, set
`service.sessionAffinity: ClientIP` and the equivalent affinity annotation on
your ingress controller. There is deliberately no HPA in this chart for the same
reason.

`terminationGracePeriodSeconds` is 30 rather than the Jenkins chart's 60: an
ITSM call is a single bounded PUT, not a poll loop waiting on a build.

## Probes

Both probes are TCP connects. The MCP endpoint rejects a bare `GET` (it expects
MCP session and `Accept` headers), so an HTTP probe would report a healthy
server as failing.

## Verify a release

```bash
kubectl -n itsm-mcp port-forward svc/itsm-mcp 8000:8000
.venv/bin/python examples/itsm_client_example.py --url http://127.0.0.1:8000/mcp
```

That lists the tools and calls the read-only `list_itsm_groups`, which exercises
config loading and the policy without touching a ticket.
