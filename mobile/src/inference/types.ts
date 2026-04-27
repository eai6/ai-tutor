// Mirrors apps/llm/client.py::BaseLLMClient — the engine talks to
// this interface, never directly to llama.rn / executorch / cloud.
// See memory/mobile_rn_plan.md "Inference abstraction".

export type InferenceRole = 'system' | 'user' | 'assistant';

export interface InferenceMessage {
  role: InferenceRole;
  content: string;
}

export interface InferenceResponse {
  content: string;
  tokens_in: number;
  tokens_out: number;
  latency_ms: number;
  tokens_per_second: number;
  model_id: string;
  generated_offline: boolean;
}

export interface GenerateOpts {
  systemPrompt?: string;
  maxTokens?: number;
  temperature?: number;
  stop?: string[];
  // Hook for early cancellation / stream-like UX. Implementations
  // are free to ignore — local llama doesn't expose mid-flight cancel
  // in the current llama.rn API.
  signal?: AbortSignal;
}

export interface InferenceClient {
  readonly modelId: string;
  readonly isOffline: boolean;

  isReady(): boolean;
  generate(messages: InferenceMessage[], opts?: GenerateOpts): Promise<InferenceResponse>;
  unload(): Promise<void>;
}

export class InferenceNotReadyError extends Error {
  constructor() {
    super('Inference client is not ready — load a model first.');
    this.name = 'InferenceNotReadyError';
  }
}
