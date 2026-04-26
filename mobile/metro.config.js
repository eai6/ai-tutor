const { getDefaultConfig } = require('expo/metro-config');

const config = getDefaultConfig(__dirname);

// expo-sqlite ships a wasm build for the web bundler — register the extension
// so Metro resolves `./wa-sqlite.wasm` and friends.
if (!config.resolver.assetExts.includes('wasm')) {
  config.resolver.assetExts.push('wasm');
}

// COEP/COOP headers are required for the wa-sqlite OPFS backend on web.
// `expo start --web` will set them when this dev-server middleware runs.
config.server = config.server || {};
const prevEnhanceMiddleware = config.server.enhanceMiddleware;
config.server.enhanceMiddleware = (middleware, server) => {
  const next = (req, res, nextFn) => {
    res.setHeader('Cross-Origin-Embedder-Policy', 'require-corp');
    res.setHeader('Cross-Origin-Opener-Policy', 'same-origin');
    middleware(req, res, nextFn);
  };
  return prevEnhanceMiddleware ? prevEnhanceMiddleware(next, server) : next;
};

module.exports = config;
