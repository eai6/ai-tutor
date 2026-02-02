# Assets Directory

This directory contains application icons and branding assets.

## Required Files for Desktop Builds

| File | Platform | Size | Format |
|------|----------|------|--------|
| `icon.ico` | Windows | 256x256 | ICO (multi-resolution) |
| `icon.icns` | macOS | 1024x1024 | ICNS (Apple Icon) |
| `icon.png` | Linux | 256x256+ | PNG |

## Creating Icons

### From a single PNG source:

1. **Create a high-resolution PNG** (1024x1024 recommended)

2. **Windows ICO** - Use ImageMagick:
   ```bash
   convert icon.png -define icon:auto-resize=256,128,64,48,32,16 icon.ico
   ```

3. **macOS ICNS** - Use iconutil (macOS):
   ```bash
   mkdir icon.iconset
   sips -z 16 16 icon.png --out icon.iconset/icon_16x16.png
   sips -z 32 32 icon.png --out icon.iconset/icon_16x16@2x.png
   sips -z 32 32 icon.png --out icon.iconset/icon_32x32.png
   sips -z 64 64 icon.png --out icon.iconset/icon_32x32@2x.png
   sips -z 128 128 icon.png --out icon.iconset/icon_128x128.png
   sips -z 256 256 icon.png --out icon.iconset/icon_128x128@2x.png
   sips -z 256 256 icon.png --out icon.iconset/icon_256x256.png
   sips -z 512 512 icon.png --out icon.iconset/icon_256x256@2x.png
   sips -z 512 512 icon.png --out icon.iconset/icon_512x512.png
   sips -z 1024 1024 icon.png --out icon.iconset/icon_512x512@2x.png
   iconutil -c icns icon.iconset
   ```

4. **Linux** - Just use the PNG directly

## Branding Guidelines

When customizing for your country:

1. Replace icons with your institution's logo
2. Ensure icons are clear at small sizes
3. Use colors from your `config/settings.json`
4. Include any required institutional branding

## Placeholder

Until you add your own icons, the build will use default placeholder icons.
You can create simple placeholder icons using:

```python
from PIL import Image, ImageDraw, ImageFont

# Create a simple placeholder icon
img = Image.new('RGB', (256, 256), color='#667eea')
draw = ImageDraw.Draw(img)
draw.text((100, 100), "AI", fill='white')
img.save('icon.png')
```
