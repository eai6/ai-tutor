import { create } from 'zustand';

import type { DownloadProgress, DownloadHandle } from '@/inference/download';
import type { OnDeviceModel } from '@/inference/catalog';

export type ModelLifecycle =
  | 'idle' // not on disk
  | 'downloading'
  | 'downloaded' // on disk, not loaded
  | 'loading'
  | 'loaded' // ready to generate
  | 'error';

interface ModelState {
  model: OnDeviceModel | null;
  status: ModelLifecycle;
  progress: DownloadProgress | null;
  loadedModelId: string | null;
  error: string | null;
  handle: DownloadHandle | null;

  setModel: (m: OnDeviceModel) => void;
  setStatus: (s: ModelLifecycle) => void;
  setProgress: (p: DownloadProgress | null) => void;
  setLoadedModelId: (id: string | null) => void;
  setError: (msg: string | null) => void;
  setHandle: (h: DownloadHandle | null) => void;
}

export const useModelStore = create<ModelState>((set) => ({
  model: null,
  status: 'idle',
  progress: null,
  loadedModelId: null,
  error: null,
  handle: null,

  setModel: (m) => set({ model: m }),
  setStatus: (s) => set({ status: s }),
  setProgress: (p) => set({ progress: p }),
  setLoadedModelId: (id) => set({ loadedModelId: id }),
  setError: (msg) => set({ error: msg }),
  setHandle: (h) => set({ handle: h }),
}));
