// Hardcoded test catalog. Once we wire to /api/v1/mobile/models/
// this becomes a server-driven list.
//
// Quality vs latency vs storage tradeoffs (rough, on a mid-range
// Android via llama.rn CPU):
//
//   0.5B Q4  →  390 MB,  ~9 tok/s,  weak instruction-following
//   0.8B Q4  →  533 MB,  ~7 tok/s,  newer arch, slightly better
//   1.5B Q4  →  990 MB,  ~5 tok/s,  reliable on most rules
//   3B Q4    →  2 GB,    ~2.5 tok/s, solid Socratic, pilot-grade

export interface OnDeviceModel {
  id: string;
  display_name: string;
  family: string;
  size_mb: number;
  ram_required_mb: number;
  download_url: string;
  filename: string;
  // Short one-liner shown in the catalog card.
  blurb: string;
  // Sort order in the UI; lower first.
  rank: number;
}

export const MODEL_CATALOG: OnDeviceModel[] = [
  {
    id: 'qwen-2-5-0-5b-q4',
    display_name: 'Qwen 2.5 0.5B (Q4)',
    family: 'qwen_2_5',
    size_mb: 390,
    ram_required_mb: 1024,
    download_url:
      'https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf?download=true',
    filename: 'qwen2.5-0.5b-instruct-q4_k_m.gguf',
    blurb: 'Smallest. Fastest. Smoke test only — weak rule-following.',
    rank: 1,
  },
  {
    id: 'qwen-3-5-0-8b-q4',
    display_name: 'Qwen 3.5 0.8B (Q4)',
    family: 'qwen_3_5',
    size_mb: 533,
    ram_required_mb: 1280,
    download_url:
      'https://huggingface.co/unsloth/Qwen3.5-0.8B-GGUF/resolve/main/Qwen3.5-0.8B-Q4_K_M.gguf?download=true',
    filename: 'Qwen3.5-0.8B-Q4_K_M.gguf',
    blurb: 'Newest architecture. Multimodal-capable. Needs llama.cpp ≥ b8149.',
    rank: 2,
  },
  {
    id: 'qwen-2-5-1-5b-q4',
    display_name: 'Qwen 2.5 1.5B (Q4)',
    family: 'qwen_2_5',
    size_mb: 990,
    ram_required_mb: 2048,
    download_url:
      'https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf?download=true',
    filename: 'qwen2.5-1.5b-instruct-q4_k_m.gguf',
    blurb: 'Mid-size. Reliable on most tutoring rules.',
    rank: 3,
  },
  {
    id: 'qwen-2-5-3b-q4',
    display_name: 'Qwen 2.5 3B (Q4)',
    family: 'qwen_2_5',
    size_mb: 2000,
    ram_required_mb: 3500,
    download_url:
      'https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf?download=true',
    filename: 'qwen2.5-3b-instruct-q4_k_m.gguf',
    blurb: 'Pilot-grade. Solid Socratic chat. ~2.5 tok/s on phone.',
    rank: 4,
  },
  {
    id: 'gemma-4-e2b-q4',
    display_name: 'Gemma 4 E2B (Q4)',
    family: 'gemma_4',
    size_mb: 3110,
    ram_required_mb: 4096,
    download_url:
      'https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/resolve/main/gemma-4-E2B-it-Q4_K_M.gguf?download=true',
    filename: 'gemma-4-E2B-it-Q4_K_M.gguf',
    blurb:
      'Google, Apr 2026. Multimodal (text + image + audio). Needs phone with ≥6 GB RAM and recent llama.cpp.',
    rank: 5,
  },
  {
    id: 'gemma-4-e4b-q4',
    display_name: 'Gemma 4 E4B (Q4)',
    family: 'gemma_4',
    size_mb: 4980,
    ram_required_mb: 6144,
    download_url:
      'https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main/gemma-4-E4B-it-Q4_K_M.gguf?download=true',
    filename: 'gemma-4-E4B-it-Q4_K_M.gguf',
    blurb:
      'Largest mobile-targeted Gemma. Best quality on-device. Needs flagship phone (≥8 GB RAM).',
    rank: 6,
  },
];

/** Convenience for the chat screen — picks a default model id when
 *  multiple are downloaded but none explicitly active. */
export function pickDefaultModel(downloadedIds: string[]): OnDeviceModel | null {
  // Prefer biggest downloaded — assumes the user wouldn't download
  // it if they didn't want to use it.
  const downloaded = MODEL_CATALOG.filter((m) => downloadedIds.includes(m.id));
  if (downloaded.length === 0) return null;
  return downloaded.sort((a, b) => b.rank - a.rank)[0];
}

// Backwards-compat: the chat screen booting code still references
// SMOKE_TEST_MODEL. Keep pointing it at the first catalog entry until
// we migrate that path to use pickDefaultModel().
export const SMOKE_TEST_MODEL = MODEL_CATALOG[0];
