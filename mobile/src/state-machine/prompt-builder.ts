// Assemble the system prompt the on-device LLM sees on each turn.
// The pack's `system_prompt_template` is baked at server time and
// covers identity / philosophy / Socratic rules. This module appends
// per-session and per-turn additions: context chunks, current step
// instructions, mobile response-format reminders.

import type { ContextChunk, PolicyStep } from './types';

export interface BuildPromptArgs {
  systemPromptTemplate: string;
  contextChunks: ContextChunk[];
  currentStep: PolicyStep | null;
  totalSteps: number;
  // True when the LLM should be reminded the student JUST got an
  // answer wrong (so it doesn't open with praise).
  studentAnswerWasWrong?: boolean;
}

/**
 * Build the full system prompt for a given turn.
 *
 * Plain-prose structure — XML tags trip up small models. Reminders
 * are placed at the END so the model reads them just before
 * generating (highest salience).
 */
export function buildSystemPrompt(args: BuildPromptArgs): string {
  const parts: string[] = [args.systemPromptTemplate.trim()];

  if (args.contextChunks.length > 0) {
    parts.push('\n\nBACKGROUND FACTS (use these silently, do not lecture them at the student):');
    for (const c of args.contextChunks) {
      parts.push(`- ${c.text}`);
    }
  }

  if (args.currentStep) {
    parts.push(buildStepBlock(args.currentStep, args.totalSteps));
  }

  // Final-line reminders (highest salience). Repeated from the baked
  // prompt because small models drift after a few turns.
  parts.push('\nREMINDER: end your next reply with ONE question (?). Keep it under 60 words.');
  // Qwen 3 / 3.5 models default to thinking-out-loud with <think>...
  // </think> blocks. Tell them not to.
  parts.push('Reply directly with the tutor message. Do NOT output <think> tags or any reasoning steps.');

  if (args.studentAnswerWasWrong) {
    parts.push('REMINDER: the student\'s last answer was WRONG. Do NOT say "correct", "exactly", "right", or "great job". Give a hint, then ask one question.');
  }

  return parts.join('\n');
}

function buildStepBlock(step: PolicyStep, total: number): string {
  const phase = (step.phase || step.step_type).toUpperCase();
  const lines = [
    `\n\nCURRENT STEP: ${step.index + 1} of ${total} (${phase})`,
  ];
  if (step.concept_tag) lines.push(`Concept being taught: ${step.concept_tag}`);
  if (step.answer_type && step.answer_type !== 'none' && step.expected_answer) {
    lines.push(`Expected answer the student should reach: ${step.expected_answer}`);
  }
  return lines.join('\n');
}
