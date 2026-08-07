<#
.SYNOPSIS
    End-to-end smoke test of the running API: create a conversation, send a turn,
    and read the transcript back. Optional switches require the pagination and
    idempotency contracts.

.DESCRIPTION
    Requires the unauthenticated POC API on http://127.0.0.1:8000. The script
    refuses non-loopback targets because this API is not safe for shared exposure.
    Exercises the full path and verifies the repository's default ownership split:
    research history in Foundry Conversations, triage history in Cosmos DB.

.PARAMETER AgentName
    Which configured agent to talk to. Defaults to triage-agent.

.PARAMETER RequirePagination
    Fail unless OpenAPI advertises cursor/limit parameters for message listing.

.PARAMETER RequireIdempotency
    Fail unless OpenAPI advertises an Idempotency-Key header for sending a turn,
    then replay that turn and require the same persisted message ids.

.EXAMPLE
    .\scripts\smoke-test.ps1
    .\scripts\smoke-test.ps1 -AgentName research-agent
#>
[CmdletBinding()]
param(
    [string] $BaseUrl = 'http://127.0.0.1:8000',
    [string] $AgentName = 'triage-agent',
    [switch] $RequirePagination,
    [switch] $RequireIdempotency
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$baseUri = [uri]$BaseUrl
$loopbackAddresses = @('127.0.0.1', '::1', 'localhost')
if ($baseUri.Scheme -ne 'http' -or $baseUri.Host -notin $loopbackAddresses) {
    throw 'The unauthenticated POC smoke test accepts only an http://localhost or loopback BaseUrl.'
}

function Invoke-Api {
    param(
        [Parameter(Mandatory)][string] $Path,
        [string] $Method = 'GET',
        [string] $Body,
        [hashtable] $AdditionalHeaders = @{}
    )

    $headers = @{ 'Content-Type' = 'application/json' }
    foreach ($headerName in $AdditionalHeaders.Keys) {
        $headers[$headerName] = $AdditionalHeaders[$headerName]
    }
    $requestParameters = @{
        Uri        = "$BaseUrl$Path"
        Method     = $Method
        Headers    = $headers
        TimeoutSec = 240
    }
    if ($PSBoundParameters.ContainsKey('Body')) { $requestParameters['Body'] = $Body }

    return Invoke-RestMethod @requestParameters
}

function Get-CollectionItems {
    param([AllowNull()][object] $Response)

    if ($null -eq $Response) { return @() }
    foreach ($propertyName in @('items', 'conversations', 'messages', 'data')) {
        $property = $Response.PSObject.Properties[$propertyName]
        if ($null -ne $property) { return @($property.Value) }
    }
    return @($Response)
}

function Get-OperationParameters {
    param(
        [Parameter(Mandatory)][object] $OpenApi,
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Method
    )

    $pathProperty = $OpenApi.paths.PSObject.Properties[$Path]
    if ($null -eq $pathProperty) { return @() }
    $operationProperty = $pathProperty.Value.PSObject.Properties[$Method.ToLowerInvariant()]
    if ($null -eq $operationProperty) { return @() }
    return @($operationProperty.Value.parameters)
}

Write-Host '1. Health' -ForegroundColor Cyan
$ready = Invoke-Api -Path '/health/ready'
Write-Host "   ready: $($ready.status)  database=$($ready.detail.database)"

Write-Host '2. Agents' -ForegroundColor Cyan
$agents = Invoke-Api -Path '/api/agents'
$agentItems = Get-CollectionItems -Response $agents
Write-Host "   $($agentItems.name -join ', ')"

$openApi = Invoke-Api -Path '/openapi.json'
$messagePath = '/api/conversations/{conversation_id}/messages'
$postParameters = Get-OperationParameters -OpenApi $openApi -Path $messagePath -Method 'post'
$idempotencyParameter = @($postParameters | Where-Object {
        $_.in -eq 'header' -and $_.name -ieq 'Idempotency-Key'
    } | Select-Object -First 1)
$idempotencyHeaders = @{}
if ($idempotencyParameter.Count -gt 0) {
    $idempotencyHeaders['Idempotency-Key'] = [guid]::NewGuid().ToString()
    Write-Host '   idempotency contract advertised by OpenAPI'
}
elseif ($RequireIdempotency) {
    throw 'IDEMPOTENCY CONTRACT MISSING: OpenAPI does not advertise Idempotency-Key.'
}
else {
    Write-Host '   idempotency contract not advertised; replay check skipped'
}

$getParameters = Get-OperationParameters -OpenApi $openApi -Path $messagePath -Method 'get'
$paginationNames = @($getParameters | Where-Object {
        $_.in -eq 'query' -and $_.name -match '^(cursor|after|continuationToken|limit|pageSize)$'
    } | ForEach-Object { $_.name })
if ($paginationNames.Count -gt 0) {
    Write-Host "   pagination contract advertised: $($paginationNames -join ', ')"
}
elseif ($RequirePagination) {
    throw 'PAGINATION CONTRACT MISSING: OpenAPI does not advertise cursor/limit parameters.'
}
else {
    Write-Host '   pagination contract not advertised; cursor traversal check skipped'
}

$conversation = $null
try {
    Write-Host "3. Create conversation with '$AgentName'" -ForegroundColor Cyan
    $conversation = Invoke-Api -Path '/api/conversations' -Method POST -Body "{`"agentName`":`"$AgentName`"}"
    $expectedManager = switch ($AgentName) {
        'research-agent' { 'foundry' }
        'triage-agent' { 'cosmos' }
        default { $null }
    }
    if ($null -ne $expectedManager -and $conversation.conversationManager -ne $expectedManager) {
        throw "OWNERSHIP FAILURE: expected '$AgentName' to use '$expectedManager', got '$($conversation.conversationManager)'."
    }
    Write-Host "   id: $($conversation.id)  history=$($conversation.conversationManager)"

    Write-Host '4. Send a turn' -ForegroundColor Cyan
    $prompt = 'The export button does nothing in Safari for several enterprise customers since this morning.'
    $turn = Invoke-Api -Path "/api/conversations/$($conversation.id)/messages" -Method POST `
        -Body (@{ content = $prompt } | ConvertTo-Json -Compress) -AdditionalHeaders $idempotencyHeaders
    Write-Host ''
    Write-Host '   --- agent reply ---' -ForegroundColor Yellow
    Write-Host $turn.assistantMessage.content
    Write-Host ''

    if ($idempotencyHeaders.Count -gt 0) {
        $replayedTurn = Invoke-Api -Path "/api/conversations/$($conversation.id)/messages" -Method POST `
            -Body (@{ content = $prompt } | ConvertTo-Json -Compress) -AdditionalHeaders $idempotencyHeaders
        if (
            $replayedTurn.userMessage.id -ne $turn.userMessage.id -or
            $replayedTurn.assistantMessage.id -ne $turn.assistantMessage.id
        ) {
            throw 'IDEMPOTENCY FAILURE: replay returned different durable message ids.'
        }
        Write-Host '   replay returned the original persisted turn'
    }

    Write-Host "5. Traverse the $($conversation.conversationManager)-managed transcript" -ForegroundColor Cyan
    $messages = [System.Collections.Generic.List[object]]::new()
    $cursor = $null
    $pageCount = 0
    do {
        $path = "/api/conversations/$($conversation.id)/messages?limit=1"
        if (-not [string]::IsNullOrWhiteSpace($cursor)) {
            $path += "&cursor=$([uri]::EscapeDataString($cursor))"
        }
        $page = Invoke-Api -Path $path
        $pageItems = @(Get-CollectionItems -Response $page)
        for ($index = $pageItems.Count - 1; $index -ge 0; $index -= 1) {
            $messages.Insert(0, $pageItems[$index])
        }
        $cursor = $page.nextCursor
        $pageCount += 1
        if ($pageCount -gt 100) {
            throw 'PAGINATION FAILURE: transcript cursor did not converge.'
        }
    } while (-not [string]::IsNullOrWhiteSpace($cursor))

    $expectedIds = @($turn.userMessage.id, $turn.assistantMessage.id)
    $actualIds = @($messages | ForEach-Object { $_.id })
    $actualRoles = @($messages | ForEach-Object { $_.role })
    if (($actualIds -join ',') -ne ($expectedIds -join ',')) {
        throw "PAGINATION FAILURE: expected message ids '$($expectedIds -join ',')', got '$($actualIds -join ',')'."
    }
    if (($actualRoles -join ',') -ne 'user,assistant') {
        throw "ORDERING FAILURE: expected roles 'user,assistant', got '$($actualRoles -join ',')'."
    }
    Write-Host "   $($messages.Count) messages traversed across $pageCount page(s): $($actualRoles -join ', ')"

    Write-Host ''
    Write-Host 'Smoke test passed.' -ForegroundColor Green
}
finally {
    if ($null -ne $conversation -and -not [string]::IsNullOrWhiteSpace($conversation.id)) {
        Write-Host '6. Delete smoke-test data' -ForegroundColor Cyan
        Invoke-Api -Path "/api/conversations/$($conversation.id)" -Method DELETE | Out-Null
        Write-Host '   conversation deleted'
    }
}
