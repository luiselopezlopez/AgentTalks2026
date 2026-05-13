[CmdletBinding()]
param(
    [string]$TargetSubscriptionName = "Luise Insight 3",
    [string]$TargetResourceGroupName = "AgentTalks2026",
    [string]$Location = "eastus2",
    [string]$AppServicePlanName = "agenttalks2026-plan",
    [string]$WebAppName = "agenttalks2026-li3-d8fd5c",
    [string]$AcrName = "agenttalks26d8fd5c",
    [string]$ImageName = "agenttalks2026",
    [string]$ImageTag = "latest",
    [string]$PlanSku = "B1",
    [int]$ContainerPort = 8000,
    [string]$AiSubscriptionName = "Luise_Insight_2",
    [string]$AiResourceGroupName = "AgentTalks_2026",
    [string]$AiAccountName = "AGentTalks2026",
    [string]$AiProjectName = "AgentTalks2026",
    [string]$VoiceLiveEndpoint = "https://agenttalks2026.services.ai.azure.com/",
    [string]$VoiceLiveAgentId = "AgentTalks2026",
    [string]$VoiceLiveAgentVersion = "16",
    [string]$VoiceLiveProjectName = "AgentTalks2026",
    [string]$VoiceLiveVoice = "es-ES-AbrilNeural",
    [string]$AvatarCharacter = "Layla",
    [string]$AvatarModel = "vasa-1"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-AzCli {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [switch]$IgnoreErrors
    )

    $output = & az @Arguments 2>$null
    if ($LASTEXITCODE -ne 0) {
        if ($IgnoreErrors) {
            return $null
        }

        throw "Azure CLI command failed: az $($Arguments -join ' ')"
    }

    if ($null -eq $output) {
        return $null
    }

    return ($output -join [Environment]::NewLine)
}

function Invoke-AzJson {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [switch]$IgnoreErrors
    )

    $output = Invoke-AzCli -Arguments $Arguments -IgnoreErrors:$IgnoreErrors
    if ([string]::IsNullOrWhiteSpace($output)) {
        return $null
    }

    return $output | ConvertFrom-Json
}

function Ensure-ResourceGroup {
    $group = Invoke-AzJson -Arguments @(
        'group', 'show',
        '--name', $TargetResourceGroupName,
        '--subscription', $TargetSubscriptionName,
        '--output', 'json'
    ) -IgnoreErrors

    if ($null -eq $group) {
        Write-Host "Creating resource group $TargetResourceGroupName in $Location"
        Invoke-AzCli -Arguments @(
            'group', 'create',
            '--name', $TargetResourceGroupName,
            '--location', $Location,
            '--subscription', $TargetSubscriptionName,
            '--output', 'json'
        ) | Out-Null
    }
}

function Ensure-Acr {
    $acr = Invoke-AzJson -Arguments @(
        'acr', 'show',
        '--name', $AcrName,
        '--resource-group', $TargetResourceGroupName,
        '--subscription', $TargetSubscriptionName,
        '--output', 'json'
    ) -IgnoreErrors

    if ($null -eq $acr) {
        Write-Host "Creating ACR $AcrName"
        Invoke-AzCli -Arguments @(
            'acr', 'create',
            '--name', $AcrName,
            '--resource-group', $TargetResourceGroupName,
            '--location', $Location,
            '--sku', 'Basic',
            '--admin-enabled', 'true',
            '--subscription', $TargetSubscriptionName,
            '--output', 'json'
        ) | Out-Null
    }

    return Invoke-AzJson -Arguments @(
        'acr', 'show',
        '--name', $AcrName,
        '--resource-group', $TargetResourceGroupName,
        '--subscription', $TargetSubscriptionName,
        '--output', 'json'
    )
}

function Ensure-AppServicePlan {
    $plan = Invoke-AzJson -Arguments @(
        'appservice', 'plan', 'show',
        '--name', $AppServicePlanName,
        '--resource-group', $TargetResourceGroupName,
        '--subscription', $TargetSubscriptionName,
        '--output', 'json'
    ) -IgnoreErrors

    if ($null -eq $plan) {
        Write-Host "Creating App Service plan $AppServicePlanName"
        Invoke-AzCli -Arguments @(
            'appservice', 'plan', 'create',
            '--name', $AppServicePlanName,
            '--resource-group', $TargetResourceGroupName,
            '--location', $Location,
            '--is-linux',
            '--sku', $PlanSku,
            '--subscription', $TargetSubscriptionName,
            '--output', 'json'
        ) | Out-Null
    }
}

function Ensure-WebApp {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ImageReference
    )

    $webApp = Invoke-AzJson -Arguments @(
        'webapp', 'show',
        '--name', $WebAppName,
        '--resource-group', $TargetResourceGroupName,
        '--subscription', $TargetSubscriptionName,
        '--output', 'json'
    ) -IgnoreErrors

    if ($null -eq $webApp) {
        Write-Host "Creating Web App $WebAppName"
        Invoke-AzCli -Arguments @(
            'webapp', 'create',
            '--name', $WebAppName,
            '--resource-group', $TargetResourceGroupName,
            '--plan', $AppServicePlanName,
            '--deployment-container-image-name', $ImageReference,
            '--subscription', $TargetSubscriptionName,
            '--output', 'json'
        ) | Out-Null
    }
}

function Ensure-RoleAssignment {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SubscriptionName,
        [Parameter(Mandatory = $true)]
        [string]$Scope,
        [Parameter(Mandatory = $true)]
        [string]$RoleName,
        [Parameter(Mandatory = $true)]
        [string]$PrincipalId
    )

    $existing = Invoke-AzJson -Arguments @(
        'role', 'assignment', 'list',
        '--assignee-object-id', $PrincipalId,
        '--scope', $Scope,
        '--subscription', $SubscriptionName,
        '--query', "[?roleDefinitionName=='$RoleName']",
        '--output', 'json'
    )

    if ($null -eq $existing -or $existing.Count -eq 0) {
        Write-Host "Assigning role '$RoleName' on scope $Scope"
        Invoke-AzCli -Arguments @(
            'role', 'assignment', 'create',
            '--assignee-object-id', $PrincipalId,
            '--assignee-principal-type', 'ServicePrincipal',
            '--role', $RoleName,
            '--scope', $Scope,
            '--subscription', $SubscriptionName,
            '--output', 'json'
        ) | Out-Null
    }
}

Write-Host "Using target subscription: $TargetSubscriptionName"
Invoke-AzCli -Arguments @('account', 'set', '--subscription', $TargetSubscriptionName) | Out-Null

Ensure-ResourceGroup

$acr = Ensure-Acr
$loginServer = $acr.loginServer

Write-Host "Building image $ImageName`:$ImageTag in ACR $AcrName"
Invoke-AzCli -Arguments @(
    'acr', 'build',
    '--registry', $AcrName,
    '--image', "${ImageName}:$ImageTag",
    '--subscription', $TargetSubscriptionName,
    '.'
) -IgnoreErrors | Out-Null

$publishedTag = Invoke-AzCli -Arguments @(
    'acr', 'repository', 'show-tags',
    '--name', $AcrName,
    '--repository', $ImageName,
    '--subscription', $TargetSubscriptionName,
    '--top', '20',
    '--orderby', 'time_desc',
    '--output', 'tsv'
) -IgnoreErrors

if ([string]::IsNullOrWhiteSpace($publishedTag) -or -not ($publishedTag -split [Environment]::NewLine).Contains($ImageTag)) {
    throw "The container image $ImageName`:$ImageTag was not found in ACR $AcrName after build."
}

Ensure-AppServicePlan
$imageReference = "$loginServer/$ImageName`:$ImageTag"
Ensure-WebApp -ImageReference $imageReference

$acrCreds = Invoke-AzJson -Arguments @(
    'acr', 'credential', 'show',
    '--name', $AcrName,
    '--resource-group', $TargetResourceGroupName,
    '--subscription', $TargetSubscriptionName,
    '--output', 'json'
)
$acrUser = $acrCreds.username
$acrPassword = $acrCreds.passwords[0].value

Write-Host "Configuring custom container"
Invoke-AzCli -Arguments @(
    'webapp', 'config', 'container', 'set',
    '--name', $WebAppName,
    '--resource-group', $TargetResourceGroupName,
    '--container-image-name', $imageReference,
    '--container-registry-url', "https://$loginServer",
    '--container-registry-user', $acrUser,
    '--container-registry-password', $acrPassword,
    '--subscription', $TargetSubscriptionName,
    '--output', 'json'
) | Out-Null

Write-Host "Enabling managed identity"
$identity = Invoke-AzJson -Arguments @(
    'webapp', 'identity', 'assign',
    '--name', $WebAppName,
    '--resource-group', $TargetResourceGroupName,
    '--subscription', $TargetSubscriptionName,
    '--output', 'json'
)
$principalId = $identity.principalId

$aiAccountScope = "/subscriptions/c51f3076-774e-4088-88df-de73828df1fc/resourceGroups/$AiResourceGroupName/providers/Microsoft.CognitiveServices/accounts/$AiAccountName"
$aiProjectScope = "$aiAccountScope/projects/$AiProjectName"

Ensure-RoleAssignment -SubscriptionName $AiSubscriptionName -Scope $aiAccountScope -RoleName "Cognitive Services User" -PrincipalId $principalId
Ensure-RoleAssignment -SubscriptionName $AiSubscriptionName -Scope $aiProjectScope -RoleName "Azure AI User" -PrincipalId $principalId

Write-Host "Applying app settings"
Invoke-AzCli -Arguments @(
    'webapp', 'config', 'appsettings', 'set',
    '--name', $WebAppName,
    '--resource-group', $TargetResourceGroupName,
    '--subscription', $TargetSubscriptionName,
    '--settings',
    "WEBSITES_PORT=$ContainerPort",
    "PORT=$ContainerPort",
    'SCM_DO_BUILD_DURING_DEPLOYMENT=false',
    "AZURE_VOICELIVE_ENDPOINT=$VoiceLiveEndpoint",
    "AZURE_VOICELIVE_AGENT_ID=$VoiceLiveAgentId",
    "AZURE_VOICELIVE_AGENT_VERSION=$VoiceLiveAgentVersion",
    "AZURE_VOICELIVE_PROJECT_NAME=$VoiceLiveProjectName",
    "AZURE_VOICELIVE_VOICE=$VoiceLiveVoice",
    "AZURE_AVATAR_CHARACTER=$AvatarCharacter",
    "AZURE_AVATAR_MODEL=$AvatarModel",
    'VOICE_ENABLE_LOCAL_AUDIO=false',
    "AZURE_VOICELIVE_FOUNDRY_RESOURCE_OVERRIDE=$AiAccountName"
) | Out-Null

Write-Host "Enabling WebSockets and Always On"
Invoke-AzCli -Arguments @(
    'webapp', 'config', 'set',
    '--name', $WebAppName,
    '--resource-group', $TargetResourceGroupName,
    '--web-sockets-enabled', 'true',
    '--always-on', 'true',
    '--subscription', $TargetSubscriptionName,
    '--output', 'json'
) | Out-Null

Write-Host "Restarting app"
Invoke-AzCli -Arguments @(
    'webapp', 'restart',
    '--name', $WebAppName,
    '--resource-group', $TargetResourceGroupName,
    '--subscription', $TargetSubscriptionName
) | Out-Null

$hostName = Invoke-AzJson -Arguments @(
    'webapp', 'show',
    '--name', $WebAppName,
    '--resource-group', $TargetResourceGroupName,
    '--subscription', $TargetSubscriptionName,
    '--query', 'defaultHostName',
    '--output', 'json'
)

Write-Host "Deployment completed"
Write-Host "Web app URL: https://$hostName"
Write-Host "WebSocket URL: wss://$hostName/ws"