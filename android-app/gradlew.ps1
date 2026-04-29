#!/usr/bin/env powershell

# This script acts as a Gradle wrapper for Windows

param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$arguments
)

$GRADLE_URL = "https://services.gradle.org/distributions/gradle-8.2-bin.zip"
$GRADLE_HOME = "$env:USERPROFILE\.gradle\wrapper\dists"
$GRADLE_VERSION = "8.2"

# For now, just try to use gradle if available
if (Get-Command gradle -ErrorAction SilentlyContinue) {
    & gradle @arguments
} else {
    Write-Error "Gradle not found. Please install Android SDK with Gradle or install Gradle separately."
    exit 1
}

