{{- define "itsm-mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "itsm-mcp.fullname" -}}
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

{{- define "itsm-mcp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "itsm-mcp.labels" -}}
helm.sh/chart: {{ include "itsm-mcp.chart" . }}
{{ include "itsm-mcp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "itsm-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "itsm-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "itsm-mcp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "itsm-mcp.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Fail early, with the same message whichever template renders first — otherwise
a chart missing several values reports whichever guard Helm happened to reach.
*/}}
{{- define "itsm-mcp.validate" -}}
{{- if not .Values.itsm.url -}}
{{- fail "itsm.url is required, e.g. --set itsm.url=https://10.10.146.120" -}}
{{- end -}}
{{- if not .Values.itsm.auth.existingSecret -}}
{{- if not .Values.itsm.auth.authtoken -}}
{{- fail "Set itsm.auth.authtoken, or point itsm.auth.existingSecret at a Secret that carries it." -}}
{{- end -}}
{{- end -}}
{{- if not (has .Values.mcp.transport (list "streamable-http" "sse")) -}}
{{- fail "mcp.transport must be streamable-http or sse; stdio has no port for a Service to reach." -}}
{{- end -}}
{{- end -}}

{{/* Secret holding the ITSM authtoken — the chart's own, or one you manage. */}}
{{- define "itsm-mcp.secretName" -}}
{{- default (include "itsm-mcp.fullname" .) .Values.itsm.auth.existingSecret -}}
{{- end -}}

{{/* ConfigMap holding itsm.json — the chart's own, or one you manage. */}}
{{- define "itsm-mcp.policyConfigMapName" -}}
{{- default (printf "%s-policy" (include "itsm-mcp.fullname" .)) .Values.existingPolicyConfigMap -}}
{{- end -}}

{{/* The write policy, as the JSON the server parses. */}}
{{- define "itsm-mcp.policyJson" -}}
{{- toPrettyJson .Values.policy -}}
{{- end -}}
