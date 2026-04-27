import { apiClient } from './client';
import type { Paginated } from './types';

export interface TutorMessage {
  message: string;
  phase: string;
  media: unknown[];
  show_exit_ticket: boolean;
  exit_ticket: unknown;
  is_complete: boolean;
  step_number?: number | null;
  total_steps?: number | null;
  is_correct?: boolean | null;
  streak_count?: number | null;
  practice_score?: number | null;
  milestone?: unknown;
  artifact_html?: string | null;
}

export interface SessionTurn {
  id: number;
  role: 'student' | 'tutor' | 'system';
  content: string;
  metadata: Record<string, unknown> | null;
  created_at: string;
  generated_offline: boolean;
  offline_model_id: string | null;
  client_generated_at: string | null;
  is_flagged: boolean;
  flag_type: string | null;
}

export interface StartSessionResponse extends TutorMessage {
  session_id: number;
  resumed?: boolean;
}

// Tutor responses go through the LLM, which can take 30s+ end-to-end.
// Override the global axios timeout for these calls.
const TUTOR_TIMEOUT_MS = 120_000;

export async function startSession(lessonId: number): Promise<StartSessionResponse> {
  const res = await apiClient.post<StartSessionResponse>(
    '/sessions/',
    { lesson_id: lessonId },
    { timeout: TUTOR_TIMEOUT_MS },
  );
  return res.data;
}

export async function respond(sessionId: number, message: string): Promise<TutorMessage> {
  const res = await apiClient.post<TutorMessage>(
    `/sessions/${sessionId}/respond/`,
    { message },
    { timeout: TUTOR_TIMEOUT_MS },
  );
  return res.data;
}

export interface ExitTicketResult {
  message: string;
  phase: string;
  exit_ticket: {
    score: number;
    passed: boolean;
    results: Array<{
      question_id: number;
      correct: boolean;
      student_answer: unknown;
      correct_answer: unknown;
      explanation?: string;
    }>;
    competency?: unknown;
  };
  is_complete: boolean;
}

export async function submitExitTicket(
  sessionId: number,
  answers: unknown[],
): Promise<ExitTicketResult> {
  const res = await apiClient.post<ExitTicketResult>(
    `/sessions/${sessionId}/exit-ticket/`,
    { answers },
    { timeout: TUTOR_TIMEOUT_MS },
  );
  return res.data;
}

export async function listSessionTurns(sessionId: number): Promise<SessionTurn[]> {
  const res = await apiClient.get<Paginated<SessionTurn>>(`/sessions/${sessionId}/turns/`);
  return res.data.results;
}
