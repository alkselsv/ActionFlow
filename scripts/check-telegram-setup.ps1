$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (Test-Path .env) {
        Get-Content .env -Encoding utf8 | ForEach-Object {
            $line = $_.Trim()
            if (-not $line -or $line.StartsWith('#') -or $line -notmatch '=') {
                return
            }
            $name, $value = $line.Split('=', 2)
            $name = $name.Trim()
            if (-not $name -or [Environment]::GetEnvironmentVariable($name, 'Process')) {
                return
            }
            $value = $value.Trim()
            if ($value.Length -ge 2 -and $value[0] -eq $value[-1] -and $value[0] -in @("'", '"')) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }

    if (Test-Path .env) {
        git check-ignore -q .env
        if ($LASTEXITCODE -ne 0) {
            throw 'Repository .env is not ignored by git; fix .gitignore before adding a bot token.'
        }
    }

    $token = $env:TELEGRAM_BOT_TOKEN
    if (-not $token) {
        throw 'TELEGRAM_BOT_TOKEN is not set. Put it in local .env or the OS secret store; do not paste it into an issue or chat.'
    }

    git grep --quiet --fixed-strings -- $token 2>$null
    if ($LASTEXITCODE -eq 0) {
        throw 'The bot token appears in tracked repository files. Remove it from Git history/tree before continuing.'
    }

    $base = "https://api.telegram.org/bot$token"
    try {
        $me = Invoke-RestMethod -Uri "$base/getMe" -TimeoutSec 20
    } catch {
        throw 'Telegram getMe failed. Check the token, network and proxy; the token is not printed.'
    }
    if (-not $me.ok) {
        throw 'Telegram getMe returned ok=false.'
    }

    $chatId = $env:TELEGRAM_TEST_CHAT_ID
    if (-not $chatId) {
        throw 'TELEGRAM_TEST_CHAT_ID is not set. Add the negative ID of the dedicated test group to local .env.'
    }

    $escapedChatId = [Uri]::EscapeDataString($chatId)
    try {
        $chat = Invoke-RestMethod -Uri "$base/getChat?chat_id=$escapedChatId" -TimeoutSec 20
        $member = Invoke-RestMethod -Uri "$base/getChatMember?chat_id=$escapedChatId&user_id=$($me.result.id)" -TimeoutSec 20
    } catch {
        throw 'Telegram getChat/getChatMember failed. Check that the bot was added to the test group.'
    }
    if (-not $chat.ok -or -not $member.ok) {
        throw 'Telegram returned ok=false for the test group.'
    }

    $chatType = [string]$chat.result.type
    if ($chatType -notin @('group', 'supergroup')) {
        throw "Test chat must be a group or supergroup; got '$chatType'."
    }
    if ([string]$member.result.status -eq 'left' -or [string]$member.result.status -eq 'kicked') {
        throw 'The bot is not an active member of the test group.'
    }

    $report = [ordered]@{
        bot_id = $me.result.id
        bot_username = $me.result.username
        can_join_groups = $me.result.can_join_groups
        can_read_all_group_messages = $me.result.can_read_all_group_messages
        chat_id = $chat.result.id
        chat_title = $chat.result.title
        chat_type = $chatType
        bot_status = $member.result.status
        bot_is_admin = [bool]$member.result.can_manage_chat
        checks = [ordered]@{
            token_present = $true
            token_not_in_tracked_files = $true
            env_ignored_or_absent = $true
            group_reachable = $true
            bot_is_member = $true
        }
    }
    $report | ConvertTo-Json -Depth 4
    if (-not $report.bot_is_admin) {
        Write-Warning 'Bot is a member but not an administrator. Record this and verify required visibility rights in #28 before expecting all group messages.'
    }
} finally {
    Pop-Location
}
