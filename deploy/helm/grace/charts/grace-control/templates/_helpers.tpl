{{- define "grace-control.name" -}}
{{- printf "%s-control" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "grace-control.labels" -}}
app.kubernetes.io/name: grace-control
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/component: control-plane
app.kubernetes.io/part-of: grace
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end -}}
{{- define "grace-control.selectorLabels" -}}
app.kubernetes.io/name: grace-control
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}
{{- define "grace-control.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}
{{- end -}}
