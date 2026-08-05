<#
.SYNOPSIS
    End-to-end smoke test of the running API: create a conversation, send a turn,
    read the transcript back, and prove per-user isolation.

.DESCRIPTION
    Requires the API running on http://127.0.0.1:8000 with AUTH_MODE=dev.
    Exercises the full path: API -> Foundry hosted agent -> Cosmos DB for MongoDB.

.PARAMETER AgentName
    Which configured agent to talk to. Defaults to triage-agent.

.EXAMPLE
    .\scripts\smoke-test.ps1
    .\scripts\smoke-test.ps1 -AgentName research-agent
#>
[CmdletBinding()]
param(
    [string] $BaseUrl = 'http://127.0.0.1:8000',
    [string] $AgentName = 'triage-agent'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-Api {
    param(
        [Parameter(Mandatory)][string] $Path,
        [string] $Method = 'GET',
        [string] $User = 'alice',
        [string] $Body
    )

    $headers = @{ 'x-dev-user-id' = $User; 'Content-Type' = 'application/json' }
    $args = @{
        Uri        = "$BaseUrl$Path"
        Method     = $Method
        Headers    = $headers
        TimeoutSec = 240
    }
    if ($PSBoundParameters.ContainsKey('Body')) { $args['Body'] = $Body }

    return Invoke-RestMethod @args
}

Write-Host '1. Health' -ForegroundColor Cyan
$ready = Invoke-Api -Path '/health/ready'
Write-Host "   ready: $($ready.status)  database=$($ready.detail.database)"

Write-Host '2. Agents' -ForegroundColor Cyan
$agents = Invoke-Api -Path '/api/agents'
Write-Host "   $($agents.name -join ', ')"

Write-Host "3. Create conversation with '$AgentName' (as alice)" -ForegroundColor Cyan
$conversation = Invoke-Api -Path '/api/conversations' -Method POST -Body "{`"agentName`":`"$AgentName`"}"
Write-Host "   id: $($conversation.id)"

Write-Host '4. Send a turn' -ForegroundColor Cyan
$prompt = 'The export button does nothing in Safari for several enterprise customers since this morning.'
$turn = Invoke-Api -Path "/api/conversations/$($conversation.id)/messages" -Method POST `
    -Body (@{ content = $prompt } | ConvertTo-Json -Compress)
Write-Host ''
Write-Host '   --- agent reply ---' -ForegroundColor Yellow
Write-Host $turn.assistantMessage.content
Write-Host ''

Write-Host '5. Read the transcript back from the database' -ForegroundColor Cyan
$messages = Invoke-Api -Path "/api/conversations/$($conversation.id)/messages"
Write-Host "   $($messages.Count) messages persisted: $($messages.role -join ', ')"

Write-Host '6. Per-user isolation: bob must not see alice''s conversation' -ForegroundColor Cyan
$bobList = Invoke-Api -Path '/api/conversations' -User 'bob'
$bobCount = @($bobList).Count
Write-Host "   bob sees $bobCount conversation(s)"

$leaked = @($bobList | Where-Object { $_.id -eq $conversation.id }).Count
if ($leaked -gt 0) {
    throw "ISOLATION FAILURE: bob can see alice's conversation $($conversation.id)"
}

try {
    Invoke-Api -Path "/api/conversations/$($conversation.id)" -User 'bob' | Out-Null
    throw "ISOLATION FAILURE: bob fetched alice's conversation directly"
}
catch [Microsoft.PowerShell.Commands.HttpResponseException] {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -ne 404) { throw "Expected 404 for cross-user fetch, got $code" }
    Write-Host '   direct fetch as bob -> 404 (indistinguishable from not-found)'
}

Write-Host ''
Write-Host 'Smoke test passed.' -ForegroundColor Green
