#!/bin/bash
# create_icns.sh - Create macOS .icns icon file from PNG icons
# Run this script in the directory containing the icon PNG files

echo "🍎 Creating macOS .icns icon file..."

# Create iconset directory
mkdir -p icon.iconset

# Copy icons with correct naming for macOS
cp icon_16.png icon.iconset/icon_16x16.png
cp icon_32.png icon.iconset/icon_16x16@2x.png
cp icon_32.png icon.iconset/icon_32x32.png
cp icon_64.png icon.iconset/icon_32x32@2x.png
cp icon_128.png icon.iconset/icon_128x128.png
cp icon_256.png icon.iconset/icon_128x128@2x.png
cp icon_256.png icon.iconset/icon_256x256.png
cp icon_512.png icon.iconset/icon_256x256@2x.png
cp icon_512.png icon.iconset/icon_512x512.png
cp icon_1024.png icon.iconset/icon_512x512@2x.png

# Create .icns file
iconutil -c icns icon.iconset -o icon.icns

# Clean up
rm -rf icon.iconset

if [ -f "icon.icns" ]; then
    echo "✅ Created icon.icns successfully!"
    echo ""
    echo "Copy icon.icns to your ai-tutor/assets/ folder:"
    echo "  cp icon.icns ~/Documents/GitHub/ai-tutor/assets/"
else
    echo "❌ Failed to create icon.icns"
    echo "Make sure you're running this on macOS"
fi
