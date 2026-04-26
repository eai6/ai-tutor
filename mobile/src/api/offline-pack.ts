import { apiClient } from './client';
import type { Lesson, LessonStep } from './types';

export interface ExitTicketQuestion {
  id: number;
  question_type: string;
  question_text: string;
  option_a: string;
  option_b: string;
  option_c: string;
  option_d: string;
  correct_answer: string;
  answer_data: Record<string, unknown> | null;
  explanation: string;
  concept_tag: string;
  difficulty: string;
  order_index: number;
}

export interface ExitTicketPayload {
  id: number;
  lesson_id: number;
  passing_score: number;
  time_limit_minutes: number | null;
  instructions: string;
  questions: ExitTicketQuestion[];
}

export interface OfflinePackPolicy {
  version: number;
  lesson_id: number;
  session_states: string[];
  initial_state: string;
  steps: Array<{
    index: number;
    step_type: string;
    phase: string;
    concept_tag: string;
    answer_type: string;
    expected_answer: string;
    max_attempts: number;
    min_exchanges_before_advance: number;
  }>;
  advance_rules: Record<string, unknown>;
  transition_to_exit_ticket_when: string;
  remediation_safety_valve_exchanges: number;
}

export interface OfflinePackResponse {
  lesson_id: number;
  pack_version: number;
  created_at: string;
  policy: OfflinePackPolicy;
  content: {
    lesson: Lesson;
    steps: LessonStep[];
    exit_ticket: ExitTicketPayload | null;
  };
  media_manifest: string[];
  student_progress: unknown;
}

export async function fetchOfflinePack(
  lessonId: number,
  refresh = false,
): Promise<OfflinePackResponse> {
  const res = await apiClient.get<OfflinePackResponse>(
    `/lessons/${lessonId}/offline-pack/`,
    { params: refresh ? { refresh: 1 } : undefined },
  );
  return res.data;
}
