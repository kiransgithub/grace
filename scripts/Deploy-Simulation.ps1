#Requires -Version 7.3
<#
.SYNOPSIS
Render an isolated GRACE simulation; optionally apply to one explicit context.
.DESCRIPTION
Default is read-only Helm lint/template. -Apply is explicit mutation authorization
when this script is run by an operator. Never uses the implicit current context.
No cluster is created, destroyed, reset, or switched by this script.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._:/@-]*$')]
    [string]$Context,
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')]
    [string]$ExistingSecret,
    [ValidateSet('dev', 'qa')]
    [string]$Environment = 'dev',
    [ValidatePattern('^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')]
    [ValidateLength(1, 53)]
    [string]$Release = 'grace-dev',
    [ValidatePattern('^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')]
    [ValidateLength(1, 63)]
    [string]$Namespace = 'grace-dev',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9./:_-]*$')]
    [string]$ImageRepository = 'grace-control',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$ImageTag = 'dev',
    [switch]$Apply
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not (Get-Command helm -ErrorAction SilentlyContinue)) {
    throw 'Helm is required; no manifest was applied.'
}
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$chart = Join-Path $projectRoot 'deploy/helm/grace'
$values = Join-Path $projectRoot "deploy/environments/$Environment.yaml"
$overrides = @(
    '--values', $values,
    '--set-string', "grace-control.existingSecret=$ExistingSecret",
    '--set-string', "grace-control.image.repository=$ImageRepository",
    '--set-string', "grace-control.image.tag=$ImageTag"
)
& helm lint $chart @overrides
if ($LASTEXITCODE -ne 0) { throw 'Chart validation failed; nothing was deployed.' }
if (-not $Apply) {
    & helm template $Release $chart --namespace $Namespace @overrides
    if ($LASTEXITCODE -ne 0) { throw 'Chart rendering failed; nothing was deployed.' }
    Write-Host "Rendered only. Target would be context '$Context', namespace '$Namespace'."
    return
}
if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw 'kubectl is required for -Apply; nothing was deployed.'
}
# The namespace and token Secret are provisioned by the operator beforehand.
# The command prints the Secret NAME only, never its contents.
& kubectl --context $Context --namespace $Namespace get secret $ExistingSecret -o name
if ($LASTEXITCODE -ne 0) { throw 'Required token Secret is not available in the exact target namespace.' }
& helm upgrade --install $Release $chart --kube-context $Context --namespace $Namespace --wait --timeout 3m @overrides
if ($LASTEXITCODE -ne 0) {
    throw 'Helm apply/readiness failed. Inspect this exact namespace; no automatic delete/reset was attempted.'
}
& kubectl --context $Context --namespace $Namespace rollout status "deployment/$Release-control" --timeout=60s
if ($LASTEXITCODE -ne 0) { throw 'Control-plane rollout did not become ready.' }
Write-Host 'Simulation ready. State is volatile; no real GPUs are reserved or launched.'
