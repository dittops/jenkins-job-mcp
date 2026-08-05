{{- define "jenkins-mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "jenkins-mcp.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "jenkins-mcp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "jenkins-mcp.labels" -}}
helm.sh/chart: {{ include "jenkins-mcp.chart" . }}
{{ include "jenkins-mcp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "jenkins-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "jenkins-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "jenkins-mcp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "jenkins-mcp.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Fail early, with the same message whichever template renders first — otherwise
a chart missing several values reports whichever guard Helm happened to reach.
*/}}
{{- define "jenkins-mcp.validate" -}}
{{- if not .Values.jenkins.url -}}
{{- fail "jenkins.url is required, e.g. --set jenkins.url=http://jenkins.example.com:8080" -}}
{{- end -}}
{{- if not .Values.jenkins.auth.existingSecret -}}
{{- if not .Values.jenkins.auth.user -}}
{{- fail "Set jenkins.auth.user, or point jenkins.auth.existingSecret at a Secret that carries it." -}}
{{- end -}}
{{- if not .Values.jenkins.auth.apiToken -}}
{{- fail "Set jenkins.auth.apiToken (a Jenkins API token, not an account password), or point jenkins.auth.existingSecret at a Secret that carries it." -}}
{{- end -}}
{{- end -}}
{{- if not (has .Values.mcp.transport (list "streamable-http" "sse")) -}}
{{- fail "mcp.transport must be streamable-http or sse; stdio has no port for a Service to reach." -}}
{{- end -}}
{{- end -}}

{{/* Secret holding the Jenkins credentials — the chart's own, or one you manage. */}}
{{- define "jenkins-mcp.secretName" -}}
{{- default (include "jenkins-mcp.fullname" .) .Values.jenkins.auth.existingSecret -}}
{{- end -}}

{{/* ConfigMap holding actions.json — the chart's own, or one you manage. */}}
{{- define "jenkins-mcp.actionsConfigMapName" -}}
{{- default (printf "%s-actions" (include "jenkins-mcp.fullname" .)) .Values.existingActionsConfigMap -}}
{{- end -}}

{{/* The registry, as the JSON the server parses. */}}
{{- define "jenkins-mcp.actionsJson" -}}
{{- toPrettyJson .Values.actions -}}
{{- end -}}
