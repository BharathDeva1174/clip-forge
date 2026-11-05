param([string]$Url)
try {
    $r = Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec 5 -ErrorAction Stop
    # Invoke-RestMethod returns the deserialized body on success.
    # If we got a body back, the HTTP request succeeded (2xx).
    exit 0
} catch {
    exit 1
}
