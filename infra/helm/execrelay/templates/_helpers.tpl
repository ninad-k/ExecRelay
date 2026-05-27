{{/*
Expand the name of the chart.
*/}}
{{- define "execrelay.name" -}}
execrelay
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "execrelay.fullname" -}}
execrelay
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "execrelay.chart" -}}
execrelay-0.1.0
{{- end }}

{{/*
Common labels
*/}}
{{- define "execrelay.labels" -}}
helm.sh/chart: execrelay-0.1.0
app.kubernetes.io/name: execrelay
app.kubernetes.io/instance: execrelay
app.kubernetes.io/managed-by: Helm
{{- end }}

{{/*
Selector labels
*/}}
{{- define "execrelay.selectorLabels" -}}
app.kubernetes.io/name: execrelay
app.kubernetes.io/instance: execrelay
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "execrelay.serviceAccountName" -}}
execrelay
{{- end }}
