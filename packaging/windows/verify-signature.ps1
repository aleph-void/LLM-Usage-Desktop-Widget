# Check that each file has a valid, timestamped Authenticode signature whose certificate
# names only the organization: no street address or postal code (city, state and country
# are always in a Public Trust certificate and can't be left out).
#
#   powershell -File packaging\windows\verify-signature.ps1 <file> [<file> ...]
param([Parameter(Mandatory, ValueFromRemainingArguments)][string[]]$Paths)
$ErrorActionPreference = 'Stop'

$failed = $false
foreach ($path in $Paths) {
    $sig = Get-AuthenticodeSignature $path
    $subject = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { '' }
    Write-Host "$path"
    Write-Host "  status: $($sig.Status)"
    Write-Host "  signer: $subject"
    if ($sig.Status -ne 'Valid') {
        Write-Host "  ERROR: signature is not Valid" -ForegroundColor Red
        $failed = $true
        continue
    }
    if (-not $sig.TimeStamperCertificate) {
        # Without a timestamp the signature stops validating when the short-lived certificate expires.
        Write-Host "  ERROR: signature is not timestamped" -ForegroundColor Red
        $failed = $true
    }
    # Street is STREET (2.5.4.9); postal code is PostalCode / OID 2.5.4.17.
    if ($subject -match '(^|,\s*)(STREET|PostalCode|OID\.2\.5\.4\.9|OID\.2\.5\.4\.17)=') {
        Write-Host "  ERROR: certificate includes a street address or postal code; use a certificate" -ForegroundColor Red
        Write-Host "         profile with 'Include street address' and 'Include postal code' unticked" -ForegroundColor Red
        $failed = $true
    }
}
if ($failed) { exit 1 }
