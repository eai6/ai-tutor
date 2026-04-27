// TS port of apps/tutoring/praise_filter.py — strip affirmative
// language from a tutor response when we know the student was wrong.
// Defense-in-depth: the system prompt also tells the LLM not to
// praise wrong answers, but small models often defy that instruction.

const PRAISE_PATTERNS = [
  /\bbrilliant\b/i,
  /\bperfect\b/i,
  /\bexactly\b/i,
  /\bexcellent\b/i,
  /\bamazing\b/i,
  /\bfantastic\b/i,
  /\bwonderful\b/i,
  /\bgreat job\b/i,
  /\bnice job\b/i,
  /\bgood job\b/i,
  /\bwell done\b/i,
  /\bnicely done\b/i,
  /\byou(?:'?ve| have)?\s+got\s+it\b/i,
  /\byou got it\b/i,
  /\byou(?:'?re| are)\s+right\b/i,
  /\bthat(?:'?s| is)\s+right\b/i,
  /\bthat(?:'?s| is)\s+correct\b/i,
  /\bthat(?:'?s| is)\s+it\b/i,
  /\bspot on\b/i,
  /\bbravo\b/i,
  /\bwoo+hoo+\b/i,
  /^\s*correct[!,.]/im,
  /^\s*right[!,.]/im,
  /^\s*yes[!,.]/im,
  /^\s*indeed[!,.]/im,
];
const PRAISE_RE = new RegExp(
  PRAISE_PATTERNS.map((re) => re.source).join('|'),
  'gim',
);

const NEUTRAL_OPENER =
  "Let's check this one together — can you walk me through your steps?";

function splitFirstSentence(text: string): [string, string] {
  if (!text) return ['', ''];
  const m = /[.!?](?:\s|$)/.exec(text);
  if (!m) return [text, ''];
  const end = m.index + m[0].length;
  return [text.slice(0, end).trimEnd(), text.slice(end).trimStart()];
}

function countMatches(re: RegExp, text: string): number {
  re.lastIndex = 0;
  return (text.match(re) || []).length;
}

export interface PraiseFilterResult {
  text: string;
  modified: boolean;
}

export function stripPraiseIfWrong(
  responseText: string,
  isCorrect: boolean | null,
): PraiseFilterResult {
  if (isCorrect === true || isCorrect == null) {
    return { text: responseText, modified: false };
  }
  if (!responseText) return { text: responseText, modified: false };

  const [firstOrig, restOrig] = splitFirstSentence(responseText);
  const firstHits = countMatches(PRAISE_RE, firstOrig);
  const restHits = restOrig ? countMatches(PRAISE_RE, restOrig) : 0;

  if (firstHits === 0 && restHits === 0) {
    return { text: responseText, modified: false };
  }

  const heavyPraise =
    firstHits > 2 || (firstHits >= 1 && firstOrig.trim().length < 40);

  // Strip praise patterns from rest globally.
  let rest = restOrig.replace(PRAISE_RE, '').replace(/\s{2,}/g, ' ').trim();

  let first: string;
  if (heavyPraise) {
    first = NEUTRAL_OPENER;
  } else {
    first = firstOrig.replace(PRAISE_RE, '').replace(/\s{2,}/g, ' ').trim();
    if (!first || first.length < 6) first = NEUTRAL_OPENER;
  }

  // Tidy stray punctuation produced by stripping mid-sentence.
  first = first.replace(/^\s*[,.!?]+\s*/, '').replace(/\s+([,.!?])/g, '$1');
  rest = rest.replace(/^\s*[,.!?]+\s*/, '');

  const out = rest ? `${first} ${rest}`.trim() : first;
  return { text: out, modified: out !== responseText };
}
