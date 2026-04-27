import { llamaClient } from './llama-client';
import { mockClient } from './mock-client';
import type { InferenceClient } from './types';

/**
 * Resolve which `InferenceClient` the engine should use right now.
 *
 * Priority:
 *  1. If a real on-device model is loaded → llama.rn
 *  2. Otherwise → mock client (engine still runs end-to-end; the
 *     student sees stub responses and a "no model loaded" banner)
 *
 * Cloud-LLM-as-fallback isn't here yet. The current online tutor
 * path uses the server-orchestrated `/sessions/<id>/respond/`
 * endpoint, which is a different mental model than client-driven
 * generation. We'll add a `CloudInferenceClient` if we ever expose a
 * raw "generate text" backend endpoint.
 */
export function resolveClient(): InferenceClient {
  if (llamaClient.isReady()) return llamaClient;
  return mockClient;
}

/**
 * Engine-test helper: get the real on-device client even if it's
 * not ready, so callers can introspect / load it manually.
 */
export function getOnDeviceClient(): InferenceClient {
  return llamaClient;
}
