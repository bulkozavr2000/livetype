# Synthesizes Russian test speech with the Windows "Irina" voice -> 16 kHz mono WAV.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\make_test_audio.ps1 <in.txt> <out.wav>
# In the text file, "|" marks a long pause (to test pause detection).
param([string]$InFile, [string]$OutFile)
Add-Type -AssemblyName System.Speech
$text = (Get-Content -Raw -Encoding UTF8 $InFile).Trim()
$parts = $text -split '\|'
$ssml = '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="ru-RU">'
foreach ($p in $parts) {
    $ssml += [System.Security.SecurityElement]::Escape($p.Trim()) + '<break time="1200ms"/>'
}
$ssml += '</speak>'
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ru-RU' } | Select-Object -First 1
if (-not $voice) { throw 'No ru-RU voice installed' }
$s.SelectVoice($voice.VoiceInfo.Name)
$s.Rate = 1
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($OutFile, $fmt)
$s.SpeakSsml($ssml)
$s.Dispose()
"OK: $OutFile ($((Get-Item $OutFile).Length) bytes, voice $($voice.VoiceInfo.Name))"
