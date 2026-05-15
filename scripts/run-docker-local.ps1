[CmdletBinding()]
param(
    [string]$ImageName = "agenttalks2026-local",
    [string]$ContainerName = "agenttalks2026-local",
    [int]$HostPort = 8000,
    [string]$EnvFile = "luiseagent/.env",
    [switch]$SkipBuild,
    [switch]$Detach
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$resolvedEnvFile = Join-Path $repoRoot $EnvFile
$logsDir = Join-Path $repoRoot "luiseagent/logs"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI no esta disponible. Instala Docker Desktop y vuelve a intentarlo."
}

if (-not (Test-Path $resolvedEnvFile)) {
    throw "No se encontro el archivo de entorno: $resolvedEnvFile"
}

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

$dockerInfo = docker info 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop no parece estar en ejecucion. Inicia Docker Desktop y vuelve a intentarlo."
}

Push-Location $repoRoot
try {
    $localImageId = (docker images -q $ImageName | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo comprobar si la imagen $ImageName existe localmente."
    }

    if (-not $SkipBuild) {
        Write-Host "Building image $ImageName"
        docker build -t $ImageName .
        if ($LASTEXITCODE -ne 0) {
            throw "docker build fallo."
        }
    }
    elseif ([string]::IsNullOrWhiteSpace($localImageId)) {
        throw "La imagen $ImageName no existe localmente. Ejecuta el script sin -SkipBuild la primera vez."
    }

    $existingContainerId = docker ps -aq --filter "name=^${ContainerName}$"
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo comprobar si ya existe el contenedor $ContainerName."
    }

    if (-not [string]::IsNullOrWhiteSpace(($existingContainerId | Out-String).Trim())) {
        Write-Host "Removing existing container $ContainerName"
        docker rm -f $ContainerName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "No se pudo eliminar el contenedor existente $ContainerName."
        }
    }

    $runArgs = @(
        "run",
        "--name", $ContainerName,
        "--rm",
        "--env-file", $resolvedEnvFile,
        "-e", "PORT=8000",
        "-p", "${HostPort}:8000",
        "-v", "${logsDir}:/app/luiseagent/logs"
    )

    if ($Detach) {
        $runArgs += "-d"
    }

    $runArgs += $ImageName

    Write-Host "Starting container $ContainerName on http://localhost:$HostPort"
    docker @runArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker run fallo."
    }

    if ($Detach) {
        Write-Host "Container started in detached mode."
        Write-Host "Logs: docker logs -f $ContainerName"
        Write-Host "Stop: docker stop $ContainerName"
    }
    else {
        Write-Host "Container stopped."
    }

    Write-Warning "Dentro de Docker no se reutiliza automaticamente tu sesion 'az login'. Si el agente necesita autenticacion Entra ID, añade AZURE_CLIENT_ID, AZURE_TENANT_ID y AZURE_CLIENT_SECRET al archivo .env o usa otra credencial soportada por DefaultAzureCredential."
}
finally {
    Pop-Location
}