#!/bin/bash
# build_mac.sh - Build AI Tutor Mac App with DMG installer
# Run from the ai-tutor project root directory

set -e  # Exit on error

echo "🍎 AI Tutor Mac App Builder"
echo "=========================="
echo ""

# Check we're in the right directory
if [ ! -f "app.py" ]; then
    echo "❌ Error: Run this script from the ai-tutor project root"
    echo "   cd ~/Documents/GitHub/ai-tutor"
    echo "   ./build_mac.sh"
    exit 1
fi

# Check for virtual environment
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "🔄 Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "📦 Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller pywebview pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-WebKit

# Check for .env file
if [ ! -f ".env" ]; then
    echo ""
    echo "⚠️  No .env file found!"
    echo "   Create one with your Anthropic API key:"
    echo ""
    echo "   echo 'ANTHROPIC_API_KEY=sk-ant-your-key-here' > .env"
    echo ""
    read -p "Do you want to continue without .env? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Create assets directory if needed
mkdir -p assets

# Check for icon
if [ ! -f "assets/icon.icns" ]; then
    echo "⚠️  No icon.icns found in assets/"
    
    # Check if we have PNG icons to convert
    if [ -f "assets/icon_1024.png" ]; then
        echo "🎨 Creating .icns from PNG icons..."
        mkdir -p assets/icon.iconset
        cp assets/icon_16.png assets/icon.iconset/icon_16x16.png 2>/dev/null || true
        cp assets/icon_32.png assets/icon.iconset/icon_16x16@2x.png 2>/dev/null || true
        cp assets/icon_32.png assets/icon.iconset/icon_32x32.png 2>/dev/null || true
        cp assets/icon_64.png assets/icon.iconset/icon_32x32@2x.png 2>/dev/null || true
        cp assets/icon_128.png assets/icon.iconset/icon_128x128.png 2>/dev/null || true
        cp assets/icon_256.png assets/icon.iconset/icon_128x128@2x.png 2>/dev/null || true
        cp assets/icon_256.png assets/icon.iconset/icon_256x256.png 2>/dev/null || true
        cp assets/icon_512.png assets/icon.iconset/icon_256x256@2x.png 2>/dev/null || true
        cp assets/icon_512.png assets/icon.iconset/icon_512x512.png 2>/dev/null || true
        cp assets/icon_1024.png assets/icon.iconset/icon_512x512@2x.png 2>/dev/null || true
        iconutil -c icns assets/icon.iconset -o assets/icon.icns
        rm -rf assets/icon.iconset
        echo "✅ Created icon.icns"
    else
        echo "   Using default icon (add icon_1024.png to assets/ for custom icon)"
        ICON_FLAG=""
    fi
fi

# Set icon flag
if [ -f "assets/icon.icns" ]; then
    ICON_FLAG="--icon=assets/icon.icns"
else
    ICON_FLAG=""
fi

# Clean previous builds
echo "🧹 Cleaning previous builds..."
rm -rf build dist *.spec

# Build the app
echo ""
echo "🔨 Building Mac App..."
echo ""

pyinstaller --name="AI-Tutor" \
    --windowed \
    --onedir \
    $ICON_FLAG \
    --add-data="templates:templates" \
    --add-data="config:config" \
    --add-data="curricula:curricula" \
    --add-data=".env:." \
    --hidden-import=webview \
    --hidden-import=flask \
    --hidden-import=flask_cors \
    --hidden-import=anthropic \
    --hidden-import=dotenv \
    --hidden-import=jinja2 \
    --collect-all=anthropic \
    --collect-all=webview \
    --osx-bundle-identifier=org.teveta.aitutor \
    desktop.py

# Check if build succeeded
if [ ! -d "dist/AI-Tutor.app" ]; then
    echo "❌ Build failed!"
    exit 1
fi

echo ""
echo "✅ App built successfully!"
echo "   Location: dist/AI-Tutor.app"

# Create DMG
echo ""
echo "📀 Creating DMG installer..."
echo ""

# Check if create-dmg is installed
if ! command -v create-dmg &> /dev/null; then
    echo "Installing create-dmg via Homebrew..."
    if ! command -v brew &> /dev/null; then
        echo "❌ Homebrew not installed. Install it first:"
        echo '   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
        echo ""
        echo "Or manually create DMG with Disk Utility"
        exit 0
    fi
    brew install create-dmg
fi

# Remove old DMG if exists
rm -f "AI-Tutor-macOS.dmg"
rm -f "AI-Tutor-macOS-temp.dmg"

# Create DMG
create-dmg \
    --volname "AI Tutor" \
    --volicon "assets/icon.icns" \
    --window-pos 200 120 \
    --window-size 660 400 \
    --icon-size 100 \
    --icon "AI-Tutor.app" 180 190 \
    --hide-extension "AI-Tutor.app" \
    --app-drop-link 480 190 \
    --background "assets/dmg-background.png" \
    "AI-Tutor-macOS.dmg" \
    "dist/AI-Tutor.app" \
    2>/dev/null || \
create-dmg \
    --volname "AI Tutor" \
    --window-pos 200 120 \
    --window-size 660 400 \
    --icon-size 100 \
    --icon "AI-Tutor.app" 180 190 \
    --hide-extension "AI-Tutor.app" \
    --app-drop-link 480 190 \
    "AI-Tutor-macOS.dmg" \
    "dist/AI-Tutor.app"

# Check if DMG was created
if [ -f "AI-Tutor-macOS.dmg" ]; then
    echo ""
    echo "🎉 Build Complete!"
    echo "================="
    echo ""
    echo "📱 App:     dist/AI-Tutor.app"
    echo "📀 DMG:     AI-Tutor-macOS.dmg"
    echo ""
    echo "To test the app:"
    echo "   open dist/AI-Tutor.app"
    echo ""
    echo "To distribute:"
    echo "   Share AI-Tutor-macOS.dmg with users"
    echo ""
    
    # Show file sizes
    APP_SIZE=$(du -sh "dist/AI-Tutor.app" | cut -f1)
    DMG_SIZE=$(du -sh "AI-Tutor-macOS.dmg" | cut -f1)
    echo "📊 Sizes:"
    echo "   App: $APP_SIZE"
    echo "   DMG: $DMG_SIZE"
else
    echo ""
    echo "⚠️  DMG creation failed, but app was built successfully"
    echo "   You can manually create DMG using Disk Utility"
    echo "   Or install create-dmg: brew install create-dmg"
fi

echo ""
echo "Done! 🚀"
