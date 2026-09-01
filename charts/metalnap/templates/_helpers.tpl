{{- define "metalnap.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "metalnap.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "metalnap.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "metalnap.labels" -}}
app.kubernetes.io/name: {{ include "metalnap.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "metalnap.selectorLabels" -}}
app.kubernetes.io/name: {{ include "metalnap.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "metalnap.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "metalnap.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
