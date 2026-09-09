#Requires -Version 7.3
[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9./:_-]*$')]
    [string]$Image = 'grace-control:dev'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker is required. No container image was built.'
}
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$dockerfile = Join-Path $projectRoot 'docker/Dockerfile'
& docker build --file $dockerfile --tag $Image $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'Docker build failed.' }
Write-Host "Built $Image locally. It has NOT been pushed or loaded into a Kubernetes node."
