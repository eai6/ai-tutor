// On-device conversation engine. Mirrors apps/tutoring/conversational_tutor.py
// but ports only the deterministic parts; the LLM call is delegated to
// an InferenceClient so the runner doesn't care whether it's running on
// a phone (llama.rn) or against a mock for tests.

import type { InferenceClient, InferenceMessage } from '@/inference/types';

import { evaluateAnswer, type AnswerType } from './grader';
import { stripPraiseIfWrong } from './praise-filter';
import { parseMediaSignal, type MediaItem } from './media-parser';
import { buildSystemPrompt } from './prompt-builder';
import { stripThinkingBlocks } from './thinking-strip';
import {
  INITIAL_SNAPSHOT,
  type ConversationTurn,
  type EngineSnapshot,
  type OfflinePolicy,
  type PolicyStep,
  type RunnerInit,
  type SessionState,
  type TutorMessage,
} from './types';

interface AdvanceRule {
  min_exchanges?: number;
  auto_advance_after?: number;
  on_correct?: 'advance';
  cap?: number;
}

const REMEDIATION_SAFETY_VALVE_FALLBACK = 15;

export class TutorRunner {
  private readonly policy: OfflinePolicy;
  private readonly client: InferenceClient;
  private readonly mediaIdMap: Record<number, MediaItem>;
  private readonly turns: ConversationTurn[];
  private snapshot: EngineSnapshot;
  // Listener fires whenever a turn is appended — used by callers
  // (chat screen, persistence) to react without polling.
  private turnListeners: Array<(turn: ConversationTurn) => void> = [];

  constructor(args: RunnerInit & { client: InferenceClient }) {
    this.policy = args.pack.policy as unknown as OfflinePolicy;
    this.client = args.client;
    this.mediaIdMap = buildMediaIdMap(args.pack.media_manifest);
    this.snapshot = args.initialSnapshot
      ? { ...INITIAL_SNAPSHOT, ...args.initialSnapshot }
      : { ...INITIAL_SNAPSHOT };
    this.turns = args.initialTurns ? [...args.initialTurns] : [];
  }

  onTurnAppended(listener: (turn: ConversationTurn) => void): () => void {
    this.turnListeners.push(listener);
    return () => {
      this.turnListeners = this.turnListeners.filter((l) => l !== listener);
    };
  }

  getSnapshot(): EngineSnapshot {
    return { ...this.snapshot };
  }

  getTurns(): ConversationTurn[] {
    return [...this.turns];
  }

  /**
   * Generate the opening tutor message for a new session.
   * Based on step 0's teacher script + a Socratic warmup question.
   */
  async start(): Promise<TutorMessage> {
    const step = this.currentStep();
    const opening = await this.generate({
      studentInput: null,
      currentStep: step,
      isOpening: true,
    });
    return this.toTutorMessage(opening, step);
  }

  /**
   * Process a student message: evaluate, advance step machinery,
   * call LLM for the natural-language reply, return the tutor message.
   */
  async respond(studentInput: string): Promise<TutorMessage> {
    if (this.snapshot.session_state === 'completed') {
      throw new Error('Session is already complete.');
    }
    const step = this.currentStep();

    // Append student turn first so it lands in conversation history.
    this.appendTurn({
      role: 'student',
      content: studentInput,
      step_index: this.snapshot.current_step_index,
      generated_offline: false,
    });

    // Deterministic eval (when the step has an expected answer).
    const grading =
      step && step.evaluator_kind === 'deterministic'
        ? evaluateAnswer({
            answerType: step.answer_type as AnswerType,
            studentAnswer: studentInput,
            expectedAnswer: step.expected_answer,
          })
        : null;
    const isCorrect = grading ? grading.result === 'correct' : null;
    this.snapshot.last_answer_correct = isCorrect;
    this.snapshot.step_attempt_count += 1;

    // Decide step advancement before generating, so the LLM can see
    // the upcoming step's instructions in its system prompt if we
    // advanced. Auto-advance steps without expected answers after N
    // exchanges; advance answer-required steps when correct or when
    // attempts are exhausted.
    const advanced = this.maybeAdvanceStep(isCorrect);

    const stepForGeneration = this.currentStep();

    const llmResponse = await this.generate({
      studentInput,
      currentStep: stepForGeneration,
      studentAnswerWasWrong: isCorrect === false,
      isOpening: false,
    });

    return this.toTutorMessage(llmResponse, stepForGeneration, isCorrect, advanced);
  }

  // -----------------------------------------------------------------
  // Internals
  // -----------------------------------------------------------------

  private currentStep(): PolicyStep | null {
    const idx = this.snapshot.current_step_index;
    return this.policy.steps[idx] ?? null;
  }

  private async generate(opts: {
    studentInput: string | null;
    currentStep: PolicyStep | null;
    studentAnswerWasWrong?: boolean;
    isOpening: boolean;
  }): Promise<{ raw: string; clean: string; media: MediaItem | null }> {
    const systemPrompt = buildSystemPrompt({
      systemPromptTemplate: this.policy.system_prompt_template,
      contextChunks: this.policy.context_chunks ?? [],
      currentStep: opts.currentStep,
      totalSteps: this.policy.steps.length,
      studentAnswerWasWrong: opts.studentAnswerWasWrong,
    });

    const messages = this.buildMessageHistory(opts.isOpening);
    const result = await this.client.generate(messages, {
      systemPrompt,
      maxTokens: 256,
      // Small on-device models follow rules more reliably at low
      // temperature. 0.3 is a good balance between instruction-
      // following and conversational variety.
      temperature: 0.3,
    });

    // Strip Qwen 3.x <think>...</think> blocks BEFORE any other
    // processing — they're noise from the model's reasoning step,
    // not part of the tutor's reply.
    const dethought = stripThinkingBlocks(result.content);
    const parsed = parseMediaSignal(dethought, this.mediaIdMap);
    const filtered = stripPraiseIfWrong(parsed.cleanText, opts.studentAnswerWasWrong ?? null);

    this.appendTurn({
      role: 'tutor',
      content: filtered.text,
      step_index: this.snapshot.current_step_index,
      generated_offline: result.generated_offline,
      offline_model_id: result.model_id,
      media: parsed.media,
    });
    this.snapshot.step_exchange_count += 1;
    this.snapshot.total_exchange_count += 1;

    return {
      raw: result.content,
      clean: filtered.text,
      media: parsed.media,
    };
  }

  private buildMessageHistory(isOpening: boolean): InferenceMessage[] {
    if (isOpening) {
      return [
        {
          role: 'user',
          content:
            'Open the lesson with a brief greeting + one Socratic question to surface the student\'s prior knowledge.',
        },
      ];
    }
    // Use the last ~10 turns so the LLM has recent context without
    // blowing the context window. step_exchange_count tracks position
    // for safety-valve advancement.
    return this.turns.slice(-10).map((t) => ({
      role: t.role === 'student' ? 'user' : 'assistant',
      content: t.content,
    }));
  }

  private appendTurn(t: Omit<ConversationTurn, 'client_uuid' | 'client_generated_at'>) {
    const full: ConversationTurn = {
      ...t,
      client_uuid: cryptoRandomId(),
      client_generated_at: new Date().toISOString(),
    };
    this.turns.push(full);
    for (const listener of this.turnListeners) {
      try {
        listener(full);
      } catch {
        // listener errors shouldn't break the engine
      }
    }
  }

  private toTutorMessage(
    g: { clean: string; media: MediaItem | null },
    step: PolicyStep | null,
    isCorrect: boolean | null = null,
    advanced = false,
  ): TutorMessage {
    return {
      content: g.clean,
      step_number: (step?.index ?? this.policy.steps.length) + 1,
      total_steps: this.policy.steps.length,
      phase: step?.phase ?? 'completed',
      is_correct: isCorrect,
      is_complete: this.snapshot.session_state === 'completed',
      show_exit_ticket: this.snapshot.session_state === 'exit_ticket',
      media: g.media,
    };
  }

  /**
   * Decide whether to bump current_step_index. Returns true when we
   * advanced. Mirrors the safety-valve logic from
   * conversational_tutor._should_advance_step.
   */
  private maybeAdvanceStep(isCorrect: boolean | null): boolean {
    const step = this.currentStep();
    if (!step) {
      this.snapshot.session_state = 'completed';
      return false;
    }
    const rule = (this.policy.advance_rules as Record<string, AdvanceRule>)[step.step_type] ?? {};
    const minExch = rule.min_exchanges ?? step.min_exchanges_before_advance ?? 1;
    const autoAdvanceAfter = rule.auto_advance_after;
    const hasAnswer = step.evaluator_kind !== 'none';
    const exch = this.snapshot.step_exchange_count;

    let shouldAdvance = false;

    if (!hasAnswer) {
      // Teach / summary-style. Advance after N exchanges.
      if (autoAdvanceAfter != null && exch + 1 >= autoAdvanceAfter) {
        shouldAdvance = true;
      }
    } else {
      // Practice / quiz / answer-required step.
      if (isCorrect && exch + 1 >= minExch) shouldAdvance = true;
      // Cap prevents getting stuck on a step with a model that can't
      // judge correctness (or a student who's stuck).
      const cap = rule.cap ?? step.max_attempts ?? 4;
      if (this.snapshot.step_attempt_count >= cap) shouldAdvance = true;
    }

    // Hard safety valve at total exchange count.
    const safety =
      this.policy.remediation_safety_valve_exchanges ?? REMEDIATION_SAFETY_VALVE_FALLBACK;
    if (this.snapshot.total_exchange_count >= safety) shouldAdvance = true;

    if (!shouldAdvance) return false;

    this.snapshot.current_step_index += 1;
    this.snapshot.step_exchange_count = 0;
    this.snapshot.step_attempt_count = 0;

    if (this.snapshot.current_step_index >= this.policy.steps.length) {
      this.snapshot.session_state = 'exit_ticket';
    }
    return true;
  }
}

function buildMediaIdMap(manifest: string[]): Record<number, MediaItem> {
  const map: Record<number, MediaItem> = {};
  manifest.forEach((url, idx) => {
    map[idx + 1] = { id: idx + 1, url };
  });
  return map;
}

function cryptoRandomId(): string {
  // Lightweight UUID-ish; doesn't need to be a real UUID for client-only
  // history. Sync layer already accepts any unique string in metadata.
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
