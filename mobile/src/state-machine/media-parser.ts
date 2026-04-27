// TS port of `_parse_media_signal` from
// apps/tutoring/conversational_tutor.py — strips |||MEDIA:N||| or
// |||GENERATE:category:description||| signals from the LLM output and
// returns the clean text plus the resolved media reference. Signals
// must NEVER reach the chat UI or DB.

const MEDIA_RE = /\|\|\|MEDIA\s*:\s*(\d+)\s*\|\|\|/;
const GENERATE_RE = /\|\|\|GENERATE\s*:\s*(\w+)\s*:\s*(.+?)\s*\|\|\|/;
// Defense-in-depth: legacy [SHOW_MEDIA:title] format from earlier
// engine versions. Strip if it ever leaks back.
const LEGACY_SHOW_RE = /\[SHOW_MEDIA:[^\]]+\]/g;

export interface MediaItem {
  id: number;
  url: string;
  title?: string;
  source?: string;
}

export interface GenerateRequest {
  category: string;
  description: string;
}

export interface MediaParseResult {
  cleanText: string;
  media: MediaItem | null;
  generate: GenerateRequest | null;
}

/**
 * Parse out media + generate signals from a tutor response.
 * @param text The raw LLM output
 * @param mediaIdMap Map of integer ID -> media item (built from the
 *   pack's media manifest at session start)
 */
export function parseMediaSignal(
  text: string,
  mediaIdMap: Record<number, MediaItem>,
): MediaParseResult {
  // Existing-media signal first.
  const m = MEDIA_RE.exec(text);
  if (m) {
    const cleaned = text.slice(0, m.index).replace(LEGACY_SHOW_RE, '').trimEnd();
    const id = parseInt(m[1], 10);
    if (id === 0) return { cleanText: cleaned, media: null, generate: null };
    return { cleanText: cleaned, media: mediaIdMap[id] ?? null, generate: null };
  }

  // Generation request — on-device tutor can't fulfil this; runner
  // logs and ignores. Signal still has to be stripped.
  const g = GENERATE_RE.exec(text);
  if (g) {
    return {
      cleanText: text.slice(0, g.index).replace(LEGACY_SHOW_RE, '').trimEnd(),
      media: null,
      generate: { category: g[1].toLowerCase(), description: g[2].trim() },
    };
  }

  return {
    cleanText: text.replace(LEGACY_SHOW_RE, '').trimEnd(),
    media: null,
    generate: null,
  };
}
