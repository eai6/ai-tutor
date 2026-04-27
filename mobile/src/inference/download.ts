import * as FS from 'expo-file-system/legacy';

import type { OnDeviceModel } from './catalog';

const MODELS_DIR = `${FS.documentDirectory}models/`;

async function ensureModelsDir() {
  const info = await FS.getInfoAsync(MODELS_DIR);
  if (!info.exists) {
    await FS.makeDirectoryAsync(MODELS_DIR, { intermediates: true });
  }
}

export function localPathFor(model: OnDeviceModel): string {
  return `${MODELS_DIR}${model.filename}`;
}

export async function isModelDownloaded(model: OnDeviceModel): Promise<boolean> {
  const path = localPathFor(model);
  const info = await FS.getInfoAsync(path);
  return info.exists && (info.size ?? 0) > 0;
}

export async function deleteModel(model: OnDeviceModel): Promise<void> {
  const path = localPathFor(model);
  const info = await FS.getInfoAsync(path);
  if (info.exists) await FS.deleteAsync(path, { idempotent: true });
}

export interface DownloadHandle {
  promise: Promise<{ uri: string } | undefined>;
  pause: () => Promise<void>;
  cancel: () => Promise<void>;
}

export interface DownloadProgress {
  bytesWritten: number;
  totalBytes: number;
  fraction: number;
}

export function startDownload(
  model: OnDeviceModel,
  onProgress: (p: DownloadProgress) => void,
): DownloadHandle {
  const dest = localPathFor(model);
  let resumable: FS.DownloadResumable | null = null;

  const promise = (async () => {
    await ensureModelsDir();
    resumable = FS.createDownloadResumable(
      model.download_url,
      dest,
      {},
      (progressData) => {
        const total = progressData.totalBytesExpectedToWrite || 1;
        onProgress({
          bytesWritten: progressData.totalBytesWritten,
          totalBytes: total,
          fraction: progressData.totalBytesWritten / total,
        });
      },
    );
    const result = await resumable.downloadAsync();
    return result ? { uri: result.uri } : undefined;
  })();

  return {
    promise,
    pause: async () => {
      if (resumable) await resumable.pauseAsync();
    },
    cancel: async () => {
      if (resumable) await resumable.cancelAsync();
      await deleteModel(model);
    },
  };
}
