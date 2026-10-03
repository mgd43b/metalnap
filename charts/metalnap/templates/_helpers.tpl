{{- define "metalnap.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "metalnap.fullname" -}}
{{- $name := include "metalnap.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
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

{{- /*
  Is a capacity ceiling configured? Non-empty (and so true) if a query is set or
  a static value is. `static: 0` IS a ceiling -- shed everything -- so the test
  is for null, never for truthiness: `if .static` and `default` both read 0 as
  unset, in the one case that matters most.
*/ -}}
{{- define "metalnap.ceilingEnabled" -}}
{{- if or .Values.capacityCeiling.query (not (kindIs "invalid" .Values.capacityCeiling.static)) -}}true{{- end -}}
{{- end -}}

{{- define "metalnap.statusName" -}}
{{- printf "%s-status" (include "metalnap.fullname" .) -}}
{{- end -}}

{{- define "metalnap.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "metalnap.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
