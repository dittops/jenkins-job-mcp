# jenkins-mcp chart

Deploys the Jenkins MCP server as a `streamable-http` endpoint that in-cluster
MCP clients connect to at
`http://<release>-jenkins-mcp.<namespace>.svc.cluster.local:8000/mcp`.

## Install

```bash
helm upgrade --install jenkins-mcp charts/jenkins-mcp \
  --namespace mcp --create-namespace \
  --set jenkins.url=http://jenkins.example.com:8080 \
  --set jenkins.auth.user=jenkins-bot \
  --set jenkins.auth.apiToken=<api-token> \
  --set jenkins.auth.buildToken=<build-token>
```

Better, once a Secret is managed out of band — nothing sensitive then passes
through `--set` (where it lands in shell history and the Helm release):

```bash
kubectl -n mcp create secret generic jenkins-creds \
  --from-literal=JENKINS_USER=jenkins-bot \
  --from-literal=JENKINS_API_TOKEN=… \
  --from-literal=JENKINS_BUILD_TOKEN=…

helm upgrade --install jenkins-mcp charts/jenkins-mcp -n mcp \
  --set jenkins.url=http://jenkins.example.com:8080 \
  --set jenkins.auth.existingSecret=jenkins-creds
```

The chart refuses to render without `jenkins.url` and a credential source, so a
half-configured release fails at `helm template` rather than in CrashLoopBackOff.

## Values

| Key | Default | Purpose |
|---|---|---|
| `image.repository` / `image.tag` | `dittops/jenkins-mcp` / chart appVersion | Image to run |
| `replicaCount` | `1` | See [Scaling](#scaling) before raising |
| `jenkins.url` | — | **Required.** Jenkins base URL |
| `jenkins.auth.existingSecret` | `""` | Secret carrying the credentials; when empty the chart renders one from `jenkins.auth.user` / `.apiToken` / `.buildToken` |
| `jenkins.auth.userKey` / `.apiTokenKey` / `.buildTokenKey` | `JENKINS_USER` / `JENKINS_API_TOKEN` / `JENKINS_BUILD_TOKEN` | Key names inside that Secret |
| `jenkins.auth.buildTokenEnvVar` | `JENKINS_BUILD_TOKEN` | Env var the build token is exposed as — match the registry's `build_token_env` if you changed it |
| `jenkins.verifySsl`, `jenkins.timeoutSeconds`, `jenkins.poll*`, `jenkins.consoleTailLines` | as upstream | Client behaviour |
| `actions` | the three `fetch*` actions | Rendered to `actions.json` in a ConfigMap |
| `existingActionsConfigMap` | `""` | Use your own ConfigMap (key `actions.json`) instead |
| `mcp.transport` / `mcp.port` / `mcp.path` | `streamable-http` / `8000` / `/mcp` | Transport wiring; `stdio` is rejected — there would be no port to serve |
| `ingress.enabled` | `false` | Read [Security](#security) first |
| `networkPolicy.enabled` / `.from` | `false` / `[]` | Restrict who may connect |
| `extraEnv`, `extraEnvFrom`, `extraVolumes`, `extraVolumeMounts` | `[]` | Escape hatches, e.g. a private CA bundle |

## Adding an action

The registry lives in `values.yaml` and is rendered into a ConfigMap, so a new
action is a values edit and an upgrade — no image rebuild:

```yaml
actions:
  actions:
    fetchmem:
      description: Fetch memory utilisation for the device.
      parameters:
        - name: window
          required: false
          default: "5m"
```

The deployment carries a checksum annotation over the rendered JSON, so
`helm upgrade` restarts the pods when the registry changes. Remember the
registry is the **allowlist**: an action not listed here cannot be triggered.

## Security

The server has **no authentication**. Anyone who can reach the URL can trigger
Jenkins jobs against your network devices, using the credentials in the pod.

- The Service is `ClusterIP` and the Ingress is off by default. Keep it that way
  unless whatever you put in front terminates TLS *and* authenticates callers.
- Turn on `networkPolicy.enabled` and list the client pods in
  `networkPolicy.from`. With `from` empty the policy denies all ingress, which
  is a safe default but will also cut off your own clients.
- The pod runs as uid 10001, non-root, all capabilities dropped, with a
  read-only root filesystem and no service account token mounted.

For a private Jenkins CA, mount the bundle and point `SSL_CERT_FILE` at it
rather than setting `jenkins.verifySsl=false`:

```yaml
extraVolumes:
  - name: ca
    secret:
      secretName: jenkins-ca
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

Long builds are polled from inside the pod, so `terminationGracePeriodSeconds`
defaults to 60 to let a draining pod finish the wait it is in.

## Probes

Both probes are TCP connects. The MCP endpoint rejects a bare `GET` (it expects
MCP session and `Accept` headers), so an HTTP probe would report a healthy
server as failing.

## Verify a release

```bash
kubectl -n mcp port-forward svc/jenkins-mcp 8000:8000
python examples/client_example.py --url http://127.0.0.1:8000/mcp
```

That lists the tools and calls the read-only `list_jenkins_actions`, which
exercises config loading and the registry without starting a build.
