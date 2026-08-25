# test_ps1_bom_crash_windows.ps1 - PROVE the premise the BOM rule rests on.
#
# Every other gate in this repo enforces "a .ps1 must start with EF BB BF" and
# takes the REASON on trust: that Windows PowerShell 5.1 reads a BOM-less file
# as the console ANSI code page and breaks on the first non-ASCII byte.
#
# Nothing measured it. The parse check in lint.yml runs on ubuntu under pwsh 7,
# which defaults to UTF-8 and therefore CANNOT reproduce the failure - it is
# green by construction for the exact class the BOM rule exists to prevent.
# (Operating rule: a gate only catches what its own runner can express.)
#
# This suite runs on the windows-latest runner under Windows PowerShell 5.1 and
# measures the claim end to end:
#
#   1. the runner really is 5.1 Desktop edition       (premise, not assumed)
#   2. a BOM-less .ps1 carrying non-ASCII FAILS to parse under 5.1
#   3. the SAME BYTES with a BOM prepended parse clean  (red -> green)
#   4. pwsh 7 parses the BOM-less file WITHOUT error    (interpreter-dependent,
#      which is why the ubuntu parse job cannot see this)
#
# Character classes are the three the 2026-04-22 CHANGELOG entry named as having
# actually broken the installer: em dash, box-drawing, and the gear emoji.
#
# This file is itself a .ps1 in this repo, so it carries a BOM and stays
# ASCII-clean: every test character is built from a char code, never a literal.

$ErrorActionPreference = "Stop"
$failures = 0
function Fail($msg) { Write-Host "  FAIL: $msg"; $script:failures++ }
function Pass($msg) { Write-Host "  ok:   $msg" }

# --- 1. premise -------------------------------------------------------------
Write-Host "--- 1. the runner is Windows PowerShell 5.1"
if ($PSVersionTable.PSEdition -ne "Desktop") {
  Write-Host "::error::this suite is meaningless off Desktop edition; got $($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion)"
  exit 1
}
Pass "PSEdition=Desktop PSVersion=$($PSVersionTable.PSVersion)"
Write-Host "  console ANSI code page: $([System.Text.Encoding]::Default.WebName) ($([System.Text.Encoding]::Default.CodePage))"

# --- build the specimen -----------------------------------------------------
$EM   = [char]0x2014   # em dash
$BOX  = [char]0x2500   # box drawing horizontal
$GEAR = [char]0x2699   # gear
$lines = @(
  ('Write-Host "start"'),
  ('# ' + ($BOX.ToString() * 6) + ' section ' + ($BOX.ToString() * 6)),
  ('$msg = "install ' + $EM + ' complete"'),
  ('Write-Host $msg'),
  ('Write-Host "' + $GEAR + ' Meta"'),
  ('Write-Host "end"')
)
$text = ($lines -join "`r`n") + "`r`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$utf8Bom   = New-Object System.Text.UTF8Encoding($true)

$dir = Join-Path $env:TEMP ("bomproof-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $dir -Force | Out-Null
$noBom   = Join-Path $dir "no-bom.ps1"
$withBom = Join-Path $dir "with-bom.ps1"
[System.IO.File]::WriteAllText($noBom,   $text, $utf8NoBom)
[System.IO.File]::WriteAllText($withBom, $text, $utf8Bom)

$nb = [System.IO.File]::ReadAllBytes($noBom)
$wb = [System.IO.File]::ReadAllBytes($withBom)
Write-Host ""
Write-Host "--- specimen: identical content, one with EF BB BF and one without"
Write-Host ("  no-bom   first 3 bytes: {0:X2} {1:X2} {2:X2}  ({3} bytes)" -f $nb[0], $nb[1], $nb[2], $nb.Length)
Write-Host ("  with-bom first 3 bytes: {0:X2} {1:X2} {2:X2}  ({3} bytes)" -f $wb[0], $wb[1], $wb[2], $wb.Length)
if ($wb.Length - $nb.Length -ne 3) { Fail "the two files differ by something other than the 3 BOM bytes" }

# What 5.1 SEES when it decodes the BOM-less file as the ANSI code page. This is
# the mechanism, printed so the log carries the evidence and not just a verdict.
$mojibake = [System.Text.Encoding]::Default.GetString($nb)
$emLine = ($mojibake -split "`r`n") | Where-Object { $_ -like "*install*" }
Write-Host "  the em-dash line as 5.1 decodes it: $emLine"
$smart = [char]0x201D
if ($emLine -and $emLine.Contains($smart)) {
  Pass ("decoding U+2014 as ANSI produced U+201D, which PowerShell accepts as a STRING DELIMITER" )
} else {
  Write-Host "  note: no U+201D in the decoded line on this code page; the parse result below is still the verdict"
}

# --- 2 + 3. red -> green under 5.1 ------------------------------------------
function Parse-Errors($path) {
  $tokens = $null; $errors = $null
  [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors) | Out-Null
  if ($null -eq $errors) { return @() }
  return $errors
}

Write-Host ""
Write-Host "--- 2. BOM-less file under Windows PowerShell 5.1 (must FAIL)"
$e1 = Parse-Errors $noBom
if ($e1.Count -gt 0) {
  Pass "$($e1.Count) parser error(s)"
  $e1 | Select-Object -First 3 | ForEach-Object { Write-Host "        line $($_.Extent.StartLineNumber): $($_.Message)" }
} else {
  Fail "BOM-less file with an em dash, box-drawing and a gear PARSED CLEAN under 5.1 - the premise behind the whole BOM rule does not reproduce here"
}

Write-Host ""
Write-Host "--- 3. the SAME BYTES with a BOM (must PASS)"
$e2 = Parse-Errors $withBom
if ($e2.Count -eq 0) {
  Pass "0 parser errors - prepending EF BB BF is the whole difference"
} else {
  Fail "the BOM'd file also failed to parse ($($e2.Count) error(s)) - then the BOM is not the variable and this control proves nothing"
  $e2 | Select-Object -First 3 | ForEach-Object { Write-Host "        line $($_.Extent.StartLineNumber): $($_.Message)" }
}

# --- 4. why the ubuntu parse job cannot see any of this ---------------------
Write-Host ""
Write-Host "--- 4. the same BOM-less file under pwsh 7 (must parse CLEAN)"
$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
if (-not $pwsh) {
  Write-Host "  SKIP: no pwsh 7 on this runner, cannot demonstrate the interpreter split"
} else {
  $probe = Join-Path $dir "probe.ps1"
  $probeSrc = @(
    '$t = $null; $e = $null',
    ('[System.Management.Automation.Language.Parser]::ParseFile("' + ($noBom -replace '\\','\\') + '", [ref]$t, [ref]$e) | Out-Null'),
    'if ($null -eq $e) { Write-Output 0 } else { Write-Output $e.Count }'
  ) -join "`r`n"
  [System.IO.File]::WriteAllText($probe, $probeSrc, $utf8Bom)
  $out = & $pwsh.Source -NoProfile -ExecutionPolicy Bypass -File $probe
  $n = 0; [void][int]::TryParse(($out | Select-Object -Last 1), [ref]$n)
  Write-Host "  pwsh $($pwsh.Version) reports $n parser error(s) on the BOM-less file"
  if ($n -eq 0) {
    Pass "pwsh 7 parses it clean - so lint.yml's ubuntu+pwsh parse step is GREEN BY CONSTRUCTION for this class"
  } else {
    Fail "pwsh 7 also errored ($n); the ubuntu parse job may already cover this class, re-check the gate-scope claim"
  }
}

Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue
Write-Host ""
if ($failures -gt 0) { Write-Host "FAILED ($failures)"; exit 1 }
Write-Host "ALL PASS - the BOM rule's premise is measured on the platform it protects"
exit 0
