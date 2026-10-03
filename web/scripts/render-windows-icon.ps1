# Generate the program, setup and tray icons from the approved VSM wordmark.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$repo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$source = [System.Drawing.Bitmap]::FromFile((Join-Path $repo 'web\src\assets\vsm-logo.png'))
$sizes = @(16,20,24,32,40,48,64,128,256)
$frames = @()
foreach ($size in $sizes) {
    $image = [System.Drawing.Bitmap]::new($size,$size,[System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($image)
    $graphics.Clear([System.Drawing.Color]::FromArgb(17,19,21))
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    # Use the wordmark alone; the subtitle cannot be read at desktop icon sizes.
    $width = [int]($size * 0.96)
    $height = [int]($width * 205.0 / 600)
    $rectangle = [System.Drawing.Rectangle]::new([int](($size-$width)/2),[int](($size-$height)/2),$width,$height)
    $graphics.DrawImage($source,$rectangle,0,0,600,205,[System.Drawing.GraphicsUnit]::Pixel)
    $graphics.Dispose()
    $stream = [System.IO.MemoryStream]::new()
    $image.Save($stream,[System.Drawing.Imaging.ImageFormat]::Png)
    $frames += ,@{ Size=$size; Bytes=$stream.ToArray() }
    if ($size -eq 256) {
        $image.Save((Join-Path $repo 'vsm\desktop\icon.png'),[System.Drawing.Imaging.ImageFormat]::Png)
    }
    $stream.Dispose()
    $image.Dispose()
}
$source.Dispose()
$iconPath = Join-Path $repo 'packaging\windows\vsm.ico'
$file = [System.IO.File]::Create($iconPath)
$writer = [System.IO.BinaryWriter]::new($file)
try {
    $writer.Write([uint16]0)
    $writer.Write([uint16]1)
    $writer.Write([uint16]$frames.Count)
    $offset = 6 + 16*$frames.Count
    foreach ($frame in $frames) {
        $dimension = if ($frame.Size -eq 256) { 0 } else { $frame.Size }
        $writer.Write([byte]$dimension)
        $writer.Write([byte]$dimension)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]32)
        $writer.Write([uint32]$frame.Bytes.Length)
        $writer.Write([uint32]$offset)
        $offset += $frame.Bytes.Length
    }
    foreach ($frame in $frames) { $writer.Write([byte[]]$frame.Bytes) }
} finally { $writer.Dispose(); $file.Dispose() }
Write-Output "Generated $iconPath ($($sizes -join ', ') px) and vsm/desktop/icon.png"
