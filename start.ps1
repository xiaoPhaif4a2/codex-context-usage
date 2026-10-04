param(
    [double]$Warning = 70,
    [double]$Critical = 85
)

Push-Location -LiteralPath $PSScriptRoot
try {
    python -m context_indicator --project $PSScriptRoot --warning $Warning --critical $Critical
}
finally {
    Pop-Location
}
