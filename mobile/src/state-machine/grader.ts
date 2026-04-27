// TS port of apps/tutoring/grader.py — deterministic answer evaluation.
// Keep the API surface tight enough that the runner doesn't need to
// know about the specific answer types, just calls evaluateAnswer().

const UNIT_SUFFIX_RE =
  /\s*(kg|g|mg|m|cm|mm|km|l|ml|s|h|hr|hrs|min|mins|seconds?|minutes?|hours?|years?|yrs?|days?|meters?|grams?|litres?|liters?|degrees?|°|°c|°f)\b/i;
const MIXED_NUMBER_RE = /^(-?)(\d+)[\s\-_]+(\d+)\/(\d+)$/;
const FRACTION_RE = /^(-?\d+)\/(\d+)$/;

const TRUE_VARIANTS = new Set(['true', 't', 'yes', 'y', '1', 'correct']);
const FALSE_VARIANTS = new Set(['false', 'f', 'no', 'n', '0', 'incorrect', 'wrong']);

export function normalizeAnswer(answer: string): string {
  return answer.toLowerCase().trim().split(/\s+/).join(' ');
}

export function parseMathAnswer(text: string | null | undefined): number | null {
  if (text == null) return null;
  let s = String(text).trim();
  if (!s) return null;

  const isPercent = s.endsWith('%');
  if (isPercent) s = s.slice(0, -1).trim();

  if (s.startsWith('$')) s = s.slice(1).trim();

  const noUnit = s.replace(UNIT_SUFFIX_RE, '').trim();
  if (noUnit) s = noUnit;

  // Strip thousands commas (between digits, with 3-digit groups).
  s = s.replace(/(?<=\d),(?=\d{3}(\D|$))/g, '');

  // Mixed number: "3 3/4" or "-3 3/4"
  const mm = MIXED_NUMBER_RE.exec(s);
  if (mm) {
    const [, sign, whole, num, den] = mm;
    const denF = parseFloat(den);
    if (denF === 0) return null;
    let value = parseFloat(whole) + parseFloat(num) / denF;
    if (sign === '-') value = -value;
    return applyPercent(value, isPercent);
  }

  // Improper fraction: "21/4"
  const fm = FRACTION_RE.exec(s);
  if (fm) {
    const [, num, den] = fm;
    const denF = parseFloat(den);
    if (denF === 0) return null;
    return applyPercent(parseFloat(num) / denF, isPercent);
  }

  // Plain int/float
  const v = parseFloat(s);
  if (Number.isFinite(v) && /^-?[\d.]+$/.test(s.replace(/^-/, '-'))) {
    return applyPercent(v, isPercent);
  }
  return null;
}

function applyPercent(value: number, isPercent: boolean): number {
  return isPercent ? value / 100 : value;
}

export function numericEquals(a: number | null, b: number | null, tolerance = 1e-6): boolean {
  if (a == null || b == null) return false;
  if (a === b) return true;
  const denom = Math.max(Math.abs(a), Math.abs(b));
  if (denom < tolerance) return Math.abs(a - b) <= tolerance;
  return Math.abs(a - b) / denom <= tolerance;
}

export type GradeResultKind = 'correct' | 'incorrect' | 'partial';

export interface GradingOutcome {
  result: GradeResultKind;
  score: number; // 0.0 – 1.0
  feedback: string;
}

const CORRECT: GradingOutcome = { result: 'correct', score: 1, feedback: 'Correct!' };
const INCORRECT_GENERIC: GradingOutcome = {
  result: 'incorrect',
  score: 0,
  feedback: "That's not quite right.",
};

export function gradeExactMatch(student: string, expected: string): GradingOutcome {
  const sNorm = normalizeAnswer(student);
  const eNorm = normalizeAnswer(expected);
  const sLetter = student.replace(/[^a-zA-Z]/g, '').toUpperCase();
  const eLetter = expected.replace(/[^a-zA-Z]/g, '').toUpperCase();
  if (sNorm === eNorm || (sLetter && sLetter === eLetter)) return CORRECT;
  return INCORRECT_GENERIC;
}

export function gradeNumeric(
  student: string,
  expected: string,
  tolerance = 0.01,
): GradingOutcome {
  const sNum = parseMathAnswer(student);
  const eNum = parseMathAnswer(expected);
  if (sNum == null) {
    return {
      result: 'incorrect',
      score: 0,
      feedback: "I couldn't read that as a number — try again.",
    };
  }
  if (eNum == null) return gradeExactMatch(student, expected);
  return numericEquals(sNum, eNum, tolerance) ? CORRECT : INCORRECT_GENERIC;
}

export function gradeTrueFalse(student: string, expected: string): GradingOutcome {
  const s = student.toLowerCase().trim();
  const e = expected.toLowerCase().trim();
  const sIsTrue = TRUE_VARIANTS.has(s);
  const sIsFalse = FALSE_VARIANTS.has(s);
  const eIsTrue = TRUE_VARIANTS.has(e);
  if (!sIsTrue && !sIsFalse) {
    return {
      result: 'incorrect',
      score: 0,
      feedback: 'Please answer True or False.',
    };
  }
  return sIsTrue === eIsTrue ? CORRECT : INCORRECT_GENERIC;
}

export type AnswerType = 'none' | 'free_text' | 'multiple_choice' | 'short_numeric' | 'true_false';

/**
 * Single entry point the runner calls. Picks the right grader based on
 * the step's `answer_type`. Returns null when the step doesn't expect
 * an answer (`none`) or the type isn't deterministic (free_text without
 * an expected answer).
 */
export function evaluateAnswer(opts: {
  answerType: AnswerType | string;
  studentAnswer: string;
  expectedAnswer: string;
}): GradingOutcome | null {
  const t = (opts.answerType || '').toLowerCase();
  const expected = (opts.expectedAnswer || '').trim();
  if (t === 'none' || !opts.studentAnswer.trim()) return null;
  switch (t) {
    case 'multiple_choice':
      return gradeExactMatch(opts.studentAnswer, expected);
    case 'short_numeric':
      return gradeNumeric(opts.studentAnswer, expected);
    case 'true_false':
      return gradeTrueFalse(opts.studentAnswer, expected);
    case 'free_text':
      return expected ? gradeExactMatch(opts.studentAnswer, expected) : null;
    default:
      return null;
  }
}
