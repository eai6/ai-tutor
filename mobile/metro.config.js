const { getDefaultConfig } = require('expo/metro-config');

const config = getDefaultConfig(__dirname);

// expo-sqlite ships a wasm build for the web bundler — register the
// extension so Metro resolves `./wa-sqlite.wasm` and friends. (We
// fall back to a localStorage shim on web, see src/db/queries —
// SharedArrayBuffer is needed for the OPFS VFS and isn't available
// without cross-origin isolation. Keeping the resolver entry here
// prevents bundling errors if anything else imports the wasm path.)
if (!config.resolver.assetExts.includes('wasm')) {
  config.resolver.assetExts.push('wasm');
}

module.exports = config;
