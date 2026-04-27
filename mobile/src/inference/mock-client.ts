import type {
  GenerateOpts,
  InferenceClient,
  InferenceMessage,
  InferenceResponse,
} from './types';

/**
 * Stub client used in dev / tests when no real model is loaded.
 * The state-machine engine should still run end-to-end against this
 * — useful for asserting orchestration without burning real tokens.
 */
export class MockInferenceClient implements InferenceClient {
  readonly modelId = 'mock';
  readonly isOffline = true;

  isReady(): boolean {
    return true;
  }

  async generate(
    messages: InferenceMessage[],
    _opts: GenerateOpts = {},
  ): Promise<InferenceResponse> {
    const last = messages[messages.length - 1];
    const echo = last ? last.content : '';
    const reply = `[mock] I heard: "${echo.slice(0, 80)}". Tell me more.`;
    // Simulate ~5 tok/s so callers can see latency UI.
    await new Promise((r) => setTimeout(r, 200));
    const tokens = Math.max(1, Math.round(reply.length / 4));
    return {
      content: reply,
      tokens_in: messages.reduce((sum, m) => sum + Math.round(m.content.length / 4), 0),
      tokens_out: tokens,
      latency_ms: 200,
      tokens_per_second: (tokens / 200) * 1000,
      model_id: this.modelId,
      generated_offline: true,
    };
  }

  async unload(): Promise<void> {
    // no-op
  }
}

export const mockClient = new MockInferenceClient();
