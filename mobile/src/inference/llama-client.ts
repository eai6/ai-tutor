import { initLlama, type LlamaContext } from 'llama.rn';

import { localPathFor } from './download';
import type { OnDeviceModel } from './catalog';
import type {
  GenerateOpts,
  InferenceClient,
  InferenceMessage,
  InferenceResponse,
} from './types';
import { InferenceNotReadyError } from './types';

export class LocalLlamaClient implements InferenceClient {
  readonly isOffline = true;
  private ctx: LlamaContext | null = null;
  private _modelId: string | null = null;

  get modelId(): string {
    return this._modelId ?? '';
  }

  isReady(): boolean {
    return this.ctx !== null;
  }

  async load(model: OnDeviceModel): Promise<void> {
    if (this.ctx) {
      await this.ctx.release();
      this.ctx = null;
    }
    const path = localPathFor(model);
    this.ctx = await initLlama({
      model: path,
      // 8K is plenty for our slim system prompt + ~10 turn history +
      // 256-token reply. Going higher costs RAM (KV cache scales
      // linearly) without much benefit for short tutor turns.
      n_ctx: 8192,
      n_threads: 4,
      n_gpu_layers: 99,
    });
    this._modelId = model.id;
  }

  async generate(
    messages: InferenceMessage[],
    opts: GenerateOpts = {},
  ): Promise<InferenceResponse> {
    if (!this.ctx) throw new InferenceNotReadyError();
    const fullMessages: InferenceMessage[] = opts.systemPrompt
      ? [{ role: 'system', content: opts.systemPrompt }, ...messages]
      : messages;

    const t0 = Date.now();
    const result = await this.ctx.completion({
      messages: fullMessages,
      n_predict: opts.maxTokens ?? 256,
      temperature: opts.temperature ?? 0.7,
      stop: opts.stop ?? ['<|im_end|>', '</s>'],
    });
    const latency = Date.now() - t0;
    const tokensOut = result.tokens_predicted ?? 0;
    return {
      content: result.text.trim(),
      tokens_in: result.tokens_evaluated ?? 0,
      tokens_out: tokensOut,
      latency_ms: latency,
      tokens_per_second: tokensOut > 0 ? (tokensOut / latency) * 1000 : 0,
      model_id: this._modelId ?? '',
      generated_offline: true,
    };
  }

  async unload(): Promise<void> {
    if (this.ctx) {
      await this.ctx.release();
      this.ctx = null;
      this._modelId = null;
    }
  }
}

// Singleton — llama.rn doesn't support more than one context per
// process anyway, so concurrent loads aren't safe.
export const llamaClient = new LocalLlamaClient();

// Backwards-compat alias for the model-store screen. Once the
// engine port replaces direct llamaClient usage we can drop this.
export type GenerateMessage = InferenceMessage;
export type GenerateResult = {
  content: string;
  tokensIn: number;
  tokensOut: number;
  latencyMs: number;
  tokensPerSecond: number;
};
