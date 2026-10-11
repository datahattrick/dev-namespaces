{{- define "devspaces.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "devspaces.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "devspaces.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: capsule
{{- end }}

{{/*
Labels and annotations for a user-supplied object: devspaces.metadata (dict "root" $ "obj" $obj)
*/}}
{{- define "devspaces.metadata" -}}
labels:
  {{- include "devspaces.labels" .root | nindent 2 }}
  {{- with .obj.labels }}
  {{- toYaml . | nindent 2 }}
  {{- end }}
{{- with .obj.annotations }}
annotations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{- define "devspaces.reaper.name" -}}
{{- printf "%s-reaper" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Every ClusterRole the Capsule controller has to be able to bind.
*/}}
{{- define "devspaces.bindableClusterRoles" -}}
{{- $opts := .Values.capsule.manager.options.rbac -}}
{{- $roles := list .Values.names.provisionerClusterRole .Values.names.deleterClusterRole -}}
{{- $roles = concat $roles ($opts.administrationClusterRoles | default list) ($opts.promotionClusterRoles | default list) .Values.controllerRbac.extraBindableClusterRoles -}}
{{- range $name, $tnt := .Values.tenants }}
  {{- range $owner := ($tnt.spec.owners | default list) }}
    {{- $roles = concat $roles $owner.clusterRoles }}
  {{- end }}
  {{- range $rb := ($tnt.spec.additionalRoleBindings | default list) }}
    {{- $roles = append $roles $rb.clusterRoleName }}
  {{- end }}
{{- end }}
{{- toYaml ($roles | uniq | sortAlpha) }}
{{- end }}

{{/*
Fail early when values that must agree between this chart and the subchart drift apart.
*/}}
{{- define "devspaces.validate" -}}
{{- $c := .Values.capsule -}}
{{- if ne .Values.names.capsuleFullname $c.fullnameOverride }}
{{- fail "names.capsuleFullname must equal capsule.fullnameOverride" }}
{{- end }}
{{- if ne .Values.names.controllerServiceAccount $c.serviceAccount.name }}
{{- fail "names.controllerServiceAccount must equal capsule.serviceAccount.name" }}
{{- end }}
{{- if ne .Values.names.provisionerClusterRole $c.manager.options.rbac.provisioner }}
{{- fail "names.provisionerClusterRole must equal capsule.manager.options.rbac.provisioner" }}
{{- end }}
{{- if ne .Values.names.deleterClusterRole $c.manager.options.rbac.deleter }}
{{- fail "names.deleterClusterRole must equal capsule.manager.options.rbac.deleter" }}
{{- end }}
{{- if and .Values.controllerRbac.create $c.manager.rbac.create }}
{{- fail "set capsule.manager.rbac.create=false when controllerRbac.create=true, otherwise the upstream cluster-admin binding is still installed" }}
{{- end }}
{{- $calc := $c.webhooks.hooks.calculations }}
{{- if and $calc.enabled (not $calc.rules) }}
{{- fail "capsule.webhooks.hooks.calculations.enabled needs rules listing the resources counted by GlobalCustomQuotas" }}
{{- end }}
{{- if and .Values.globalCustomQuotas (not $calc.rules) }}
{{- fail "globalCustomQuotas need their source resources listed in capsule.webhooks.hooks.calculations.rules (grants the controller read access)" }}
{{- end }}
{{- range $name, $tnt := .Values.tenants }}
  {{- range $owner := ($tnt.spec.owners | default list) }}
    {{- if not $owner.clusterRoles }}
    {{- fail (printf "tenants.%s: owner %s/%s must set clusterRoles explicitly" $name $owner.kind $owner.name) }}
    {{- end }}
  {{- end }}
{{- end }}
{{- end }}
