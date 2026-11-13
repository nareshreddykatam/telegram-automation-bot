$ErrorActionPreference = 'Stop'

$repoUrl = 'https://github.com/nareshreddykatam/telegram-automation-bot.git'
$branch = 'migration/phase-1-postgres-adapter'
$source = (Resolve-Path $PSScriptRoot).Path
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('telegram-bot-publish-' + [guid]::NewGuid().ToString('N'))
$worktree = Join-Path $tempRoot 'repo'

function Stop-WithMessage([string]$message) {
    Write-Host "ERROR: $message" -ForegroundColor Red
    exit 1
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Stop-WithMessage 'Git is not installed. Install Git for Windows, reopen PowerShell, and run this script again.'
}

Write-Host 'Cloning the current public repository into a temporary folder...'
New-Item -ItemType Directory -Path $tempRoot | Out-Null
git clone --branch main --single-branch $repoUrl $worktree
if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'Could not clone the repository. Check your internet connection and GitHub access.' }

$existingBranch = git -C $worktree ls-remote --heads origin $branch
if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'Could not check whether the migration branch already exists.' }
if ($existingBranch) { Stop-WithMessage "Branch '$branch' already exists remotely. This script will not overwrite it." }

git -C $worktree switch -c $branch
if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'Could not create the migration branch.' }

Write-Host 'Copying clean project files (excluding secrets, databases, logs, caches, and local Git history)...'
$robocopyArgs = @(
    $source, $worktree, '/E', '/R:2', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS', '/NP',
    '/XD', '.git', 'data', 'logs', '.ruff_cache', '.pytest_cache', '__pycache__', '.venv', 'venv',
    '/XF', '.env', '*.db', '*.db-wal', '*.db-shm', '*.log', '*.pyc'
)
& robocopy @robocopyArgs
$copyCode = $LASTEXITCODE
if ($copyCode -ge 8) { Stop-WithMessage "File copy failed (robocopy exit code $copyCode)." }

# Explicitly ensure no real environment file or local database entered the Git worktree.
$forbidden = Get-ChildItem -LiteralPath $worktree -Force -Recurse -File | Where-Object {
    $_.Name -eq '.env' -or $_.Extension -eq '.db' -or $_.Name -like '*.db-wal' -or $_.Name -like '*.db-shm' -or $_.Extension -eq '.log'
}
if ($forbidden) {
    $forbidden | ForEach-Object { Write-Host "Excluded sensitive/local file found: $($_.FullName)" -ForegroundColor Red }
    Stop-WithMessage 'Sensitive or local runtime files were found in the temporary Git worktree. Nothing was pushed.'
}

git -C $worktree add --all
if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'Git could not stage the project files.' }

git -C $worktree diff --cached --check
if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'Git whitespace validation failed. Nothing was pushed.' }

$status = git -C $worktree status --short
if (-not $status) { Stop-WithMessage 'No project changes were detected; nothing to publish.' }

Write-Host ''
Write-Host 'Ready to publish the migration branch:' -ForegroundColor Green
Write-Host "  $branch"
Write-Host '  Target: https://github.com/nareshreddykatam/telegram-automation-bot'
Write-Host '  Existing main branch will not be changed.'
Write-Host ''

# User has already authorized this migration/publish task in the conversation.
git -C $worktree commit -m 'Add PostgreSQL, queue worker, and serverless webhook migration'
if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'Commit failed. The temporary worktree has been retained for inspection.' }

git -C $worktree push -u origin $branch
if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'Push failed. The local temporary branch has been retained; nothing was merged into main.' }

Write-Host ''
Write-Host 'SUCCESS: Migration branch pushed. No production deployment or Telegram webhook was activated.' -ForegroundColor Green
Write-Host "Review the branch at: https://github.com/nareshreddykatam/telegram-automation-bot/tree/$branch"
Write-Host "Temporary checkout: $worktree"
