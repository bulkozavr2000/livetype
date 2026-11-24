# Test target with UI Automation support: a WPF TextBox that saves its text every 150 ms.
# powershell -STA -File tests\target_wpf.ps1 -Out <file> [-InitFile <utf8 file>] [-Caret <index>]
param([string]$Out, [string]$InitFile = "", [int]$Caret = -1, [int]$W = 980, [int]$H = 260)
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase
$win = New-Object System.Windows.Window
$win.Title = "LiveType WPF test"
$win.Width = $W
$win.Height = $H
$win.WindowStartupLocation = "CenterScreen"
$tb = New-Object System.Windows.Controls.TextBox
$tb.AcceptsReturn = $true
$tb.TextWrapping = "Wrap"
$tb.FontSize = 15
$tb.Padding = New-Object System.Windows.Thickness 8
if ($InitFile) { $tb.Text = [IO.File]::ReadAllText($InitFile, [Text.Encoding]::UTF8) }
$win.Content = $tb
$utf8 = New-Object Text.UTF8Encoding $false
$win.Add_ContentRendered({
    $tb.Focus() | Out-Null
    if ($Caret -ge 0) { $tb.CaretIndex = $Caret } else { $tb.CaretIndex = $tb.Text.Length }
    $p = $tb.PointToScreen((New-Object System.Windows.Point 0, 0))
    [IO.File]::WriteAllText("$Out.json", "{""text_x"": $([int]$p.X), ""text_y"": $([int]$p.Y)}", $utf8)
})
$timer = New-Object System.Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(150)
$timer.Add_Tick({ [IO.File]::WriteAllText($Out, $tb.Text, $utf8) })
$timer.Start()
$win.ShowDialog() | Out-Null
