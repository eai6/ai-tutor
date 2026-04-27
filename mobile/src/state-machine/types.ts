// Engine types — mirrors apps/tutoring/conversational_tutor.py state.

import type { OfflinePackResponse } from '@/api/offline-pack';
import type { MediaItem } from './media-parser';

export type SessionState = 'tutoring' | 'exit_ticket' | 'completed';

export type EvaluatorKind = 'none' | 'deterministic' | 'llm' | 'hybrid';

export interface PolicyStep {
  index: number;
  step_id: number;
  step_type: string;
  phase: string;
  concept_tag: string;
  answer_type: string;
  evaluator_kind: EvaluatorKind;
  expected_answer: string;
  max_attempts: number;
  min_exchanges_before_advance: number;
}

export interface ContextChunk {
  text: string;
  source: string;
}

export interface OfflinePolicy {
  version: number;
  lesson_id: number;
  session_states: string[];
  initial_state: string;
  system_prompt_template: string;
  context_chunks: ContextChunk[];
  steps: PolicyStep[];
  advance_rules: Record<string, unknown>;
  transition_to_exit_ticket_when: string;
  remediation_safety_valve_exchanges: number;
}

export interface ConversationTurn {
  role: 'student' | 'tutor' | 'system';
  content: string;
  // Stamped at runtime, used for sync.
  client_uuid: string;
  client_generated_at: string;
  step_index: number;
  generated_offline: boolean;
  offline_model_id?: string;
  media?: MediaItem | null;
}

export interface EngineSnapshot {
  session_state: SessionState;
  current_step_index: number;
  step_exchange_count: number;
  total_exchange_count: number;
  step_attempt_count: number; // for `max_attempts` enforcement
  last_answer_correct: boolean | null;
  is_remediation: boolean;
  remediation_attempt: number;
  // Per-step concepts marked covered. Mirrors `concepts_covered` in
  // the Python engine (set, simplified to array here).
  concepts_covered: string[];
  // Map of step_index -> count of consecutive bare answers, for the
  // math-tutor "show your work" enforcement.
  bare_answer_counts: Record<number, number>;
}

export interface TutorMessage {
  content: string;
  step_number: number;
  total_steps: number;
  phase: string;
  is_correct: boolean | null;
  is_complete: boolean;
  show_exit_ticket: boolean;
  media: MediaItem | null;
}

// What the runner needs at construct time.
export interface RunnerInit {
  pack: OfflinePackResponse;
  studentName?: string;
  // Optional initial snapshot when resuming an interrupted session.
  initialSnapshot?: EngineSnapshot;
  // Optional initial turn history when resuming (LLM needs recent
  // turns to maintain context).
  initialTurns?: ConversationTurn[];
}

export const INITIAL_SNAPSHOT: EngineSnapshot = {
  session_state: 'tutoring',
  current_step_index: 0,
  step_exchange_count: 0,
  total_exchange_count: 0,
  step_attempt_count: 0,
  last_answer_correct: null,
  is_remediation: false,
  remediation_attempt: 0,
  concepts_covered: [],
  bare_answer_counts: {},
};
