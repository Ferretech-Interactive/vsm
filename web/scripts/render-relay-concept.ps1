param([switch]$Production)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$assetDir = Join-Path $PSScriptRoot '..\src\assets'
$canvas = [System.Drawing.Bitmap]::new(720,480)
$g = [System.Drawing.Graphics]::FromImage($canvas)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$background = [System.Drawing.Color]::FromArgb(17,19,21)
$g.Clear($background)
$border = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(75,85,96),2)
if (-not $Production) { $g.DrawRectangle($border,20,20,680,440) }
$word = [System.Drawing.Bitmap]::new(480,220)
$wg = [System.Drawing.Graphics]::FromImage($word)
$wg.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$red = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(171,0,0))
function Fill-Polygon($coordinates) {
    $points = for ($i=0; $i -lt $coordinates.Length; $i+=2) {
        [System.Drawing.Point]::new($coordinates[$i],$coordinates[$i+1])
    }
    $wg.FillPolygon($red,[System.Drawing.Point[]]$points)
}
Fill-Polygon @(42,15,76,15,100,173,124,15,158,15,121,205,79,205)
$wg.FillRectangle($red,175,15,98,34)
$wg.FillRectangle($red,175,15,34,97)
$wg.FillRectangle($red,175,94,92,34)
$wg.FillRectangle($red,239,110,34,95)
$wg.FillRectangle($red,175,171,98,34)
Fill-Polygon @(294,205,294,15,326,15,360,96,394,15,426,15,426,205,394,205,394,76,373,140,347,140,326,76,326,205)
$wg.Dispose()
# Glyph bounds are x=42..426; offset 126 centers their midpoint at x=360.
$originX = 126
$originY = 72
$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
$g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::Half
for ($y=0; $y -lt 220; $y++) {
    $distance = ($y-110)/23.0
    $envelope = [Math]::Exp(-0.5*$distance*$distance)
    $wave = 0.62 + 0.22*[Math]::Sin($y*0.43) + 0.16*[Math]::Sin($y*0.91)
    $shift = -[int](56*$envelope*$wave)
    # Successive translucent samples create a continuous motion-blur tail.
    for ($trail=70; $trail -ge 2; $trail-=2) {
        $alpha = 0.07*$envelope*[Math]::Pow(1-$trail/74.0,1.4)
        if ($alpha -lt 0.001) { continue }
        $attributes = [System.Drawing.Imaging.ImageAttributes]::new()
        $matrix = [System.Drawing.Imaging.ColorMatrix]::new()
        $matrix.Matrix33 = $alpha
        $attributes.SetColorMatrix($matrix)
        $g.DrawImage($word,[System.Drawing.Rectangle]::new($originX+$shift-$trail,$originY+$y,480,1),0,$y,480,1,[System.Drawing.GraphicsUnit]::Pixel,$attributes)
        $attributes.Dispose()
    }
    $g.DrawImage($word,[System.Drawing.Rectangle]::new($originX+$shift,$originY+$y,480,1),0,$y,480,1,[System.Drawing.GraphicsUnit]::Pixel)
}
$orange = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(230,126,34))
$small = [System.Drawing.Font]::new('Arial',14,[System.Drawing.FontStyle]::Bold)
if (-not $Production) { $g.DrawString('CONCEPT 01 / THE RELAY',$small,$orange,48,52) }
$caption = [System.Drawing.Font]::new('Arial',16)
$format = [System.Drawing.StringFormat]::new()
$format.Alignment = [System.Drawing.StringAlignment]::Center
$gray = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(156,163,175))
$g.DrawString('VEIN SERVER MANAGER',$caption,$gray,360,305,$format)
$g.Dispose()
if ($Production) {
    # Center the lettering and subtitle (x=360) within equal side margins.
    # The leftward smear needs 300 pixels; reserve the same space to the right.
    $logo = $canvas.Clone([System.Drawing.Rectangle]::new(60,78,600,260),$canvas.PixelFormat)
    $logo.Save((Join-Path $assetDir 'vsm-logo.png'),[System.Drawing.Imaging.ImageFormat]::Png)
    $logo.Dispose()
    $icon = [System.Drawing.Bitmap]::new(128,128)
    $ig = [System.Drawing.Graphics]::FromImage($icon)
    $ig.Clear($background)
    $ig.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $ig.DrawImage($canvas,[System.Drawing.Rectangle]::new(4,42,120,44),60,78,600,220,[System.Drawing.GraphicsUnit]::Pixel)
    $ig.Dispose()
    $publicDir = Join-Path $PSScriptRoot '..\public'
    [System.IO.Directory]::CreateDirectory($publicDir) | Out-Null
    $icon.Save((Join-Path $publicDir 'vsm-icon.png'),[System.Drawing.Imaging.ImageFormat]::Png)
    $icon.Dispose()
} else {
    $canvas.Save((Join-Path $assetDir 'concept-vsm-v4.png'),[System.Drawing.Imaging.ImageFormat]::Png)
}
foreach ($resource in @($canvas,$word,$red,$border,$orange,$small,$caption,$format,$gray)) { $resource.Dispose() }
