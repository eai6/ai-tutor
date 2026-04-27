import { useEffect, useState } from 'react';
import { Stack } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';

import {
  MODEL_CATALOG,
  type OnDeviceModel,
} from '@/inference/catalog';
import {
  deleteModel,
  isModelDownloaded,
  startDownload,
  type DownloadHandle,
  type DownloadProgress,
} from '@/inference/download';
import { llamaClient } from '@/inference/llama-client';
import { Screen } from '@/components/Screen';
import { useModelStore } from '@/stores/model-store';
import { colors, fonts, radius, spacing, typography } from '@/theme';

type Status =
  | { kind: 'idle' }
  | { kind: 'downloading'; progress: DownloadProgress; handle: DownloadHandle }
  | { kind: 'downloaded' }
  | { kind: 'loading' }
  | { kind: 'loaded' }
  | { kind: 'error'; message: string };

export default function ModelStoreScreen() {
  const setLoadedModelId = useModelStore((s) => s.setLoadedModelId);
  const setStoreStatus = useModelStore((s) => s.setStatus);

  // Per-model status map.
  const [statuses, setStatuses] = useState<Record<string, Status>>({});

  // On mount, detect which models are on disk + which is loaded.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const initial: Record<string, Status> = {};
      const loadedId = llamaClient.isReady() ? llamaClient.modelId : null;
      for (const model of MODEL_CATALOG) {
        if (loadedId === model.id) {
          initial[model.id] = { kind: 'loaded' };
        } else if (await isModelDownloaded(model)) {
          initial[model.id] = { kind: 'downloaded' };
        } else {
          initial[model.id] = { kind: 'idle' };
        }
      }
      if (!cancelled) setStatuses(initial);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  function setStatus(id: string, status: Status) {
    setStatuses((prev) => ({ ...prev, [id]: status }));
  }

  async function handleDownload(model: OnDeviceModel) {
    const handle = startDownload(model, (progress) =>
      setStatus(model.id, { kind: 'downloading', progress, handle }),
    );
    setStatus(model.id, {
      kind: 'downloading',
      progress: { bytesWritten: 0, totalBytes: model.size_mb * 1_000_000, fraction: 0 },
      handle,
    });
    try {
      await handle.promise;
      setStatus(model.id, { kind: 'downloaded' });
    } catch (e) {
      setStatus(model.id, {
        kind: 'error',
        message: e instanceof Error ? e.message : 'Download failed',
      });
    }
  }

  async function handleCancel(model: OnDeviceModel) {
    const cur = statuses[model.id];
    if (cur?.kind !== 'downloading') return;
    await cur.handle.cancel();
    setStatus(model.id, { kind: 'idle' });
  }

  async function handleLoad(model: OnDeviceModel) {
    // Loading a new model unloads any other loaded model (llama.rn
    // singleton). Mark the previously-loaded model as 'downloaded'.
    const previouslyLoaded = Object.keys(statuses).find(
      (id) => statuses[id]?.kind === 'loaded' && id !== model.id,
    );
    if (previouslyLoaded) {
      setStatus(previouslyLoaded, { kind: 'downloaded' });
    }
    setStatus(model.id, { kind: 'loading' });
    setStoreStatus('loading');
    try {
      await llamaClient.load(model);
      setLoadedModelId(model.id);
      setStoreStatus('loaded');
      setStatus(model.id, { kind: 'loaded' });
    } catch (e) {
      setStoreStatus('error');
      setStatus(model.id, {
        kind: 'error',
        message: e instanceof Error ? e.message : 'Load failed',
      });
    }
  }

  async function handleUnload(model: OnDeviceModel) {
    await llamaClient.unload();
    setLoadedModelId(null);
    setStoreStatus('downloaded');
    setStatus(model.id, { kind: 'downloaded' });
  }

  async function handleDelete(model: OnDeviceModel) {
    if (statuses[model.id]?.kind === 'loaded') {
      await llamaClient.unload();
      setLoadedModelId(null);
      setStoreStatus('idle');
    }
    await deleteModel(model);
    setStatus(model.id, { kind: 'idle' });
  }

  return (
    <Screen scroll={false}>
      <Stack.Screen options={{ title: 'AI Models' }} />
      <ScrollView contentContainerStyle={styles.content}>
        <View style={styles.hero}>
          <Text style={[typography.caption, styles.muted]}>ON-DEVICE TUTOR</Text>
          <Text style={typography.largeTitle}>Pick a model</Text>
          <Text style={[typography.body, styles.objective]}>
            One model can be loaded at a time. Bigger = smarter, but slower
            and larger to download.
          </Text>
        </View>

        {MODEL_CATALOG.map((model) => (
          <ModelCard
            key={model.id}
            model={model}
            status={statuses[model.id] ?? { kind: 'idle' }}
            onDownload={() => handleDownload(model)}
            onCancel={() => handleCancel(model)}
            onLoad={() => handleLoad(model)}
            onUnload={() => handleUnload(model)}
            onDelete={() => handleDelete(model)}
          />
        ))}
      </ScrollView>
    </Screen>
  );
}

interface ModelCardProps {
  model: OnDeviceModel;
  status: Status;
  onDownload: () => void;
  onCancel: () => void;
  onLoad: () => void;
  onUnload: () => void;
  onDelete: () => void;
}

function ModelCard({
  model,
  status,
  onDownload,
  onCancel,
  onLoad,
  onUnload,
  onDelete,
}: ModelCardProps) {
  return (
    <View style={[styles.card, status.kind === 'loaded' ? styles.cardActive : null]}>
      <View style={styles.cardHead}>
        <View style={{ flex: 1, gap: 4 }}>
          <View style={styles.titleRow}>
            <Text style={typography.h3} numberOfLines={1}>
              {model.display_name}
            </Text>
            {status.kind === 'loaded' ? (
              <View style={styles.loadedPill}>
                <View style={styles.loadedDot} />
                <Text style={styles.loadedPillText}>ACTIVE</Text>
              </View>
            ) : null}
          </View>
          <Text style={[typography.small, styles.muted]} numberOfLines={2}>
            {model.blurb}
          </Text>
          <View style={styles.specs}>
            <Spec label={`${model.size_mb} MB`} />
            <Spec label={`${model.ram_required_mb} MB RAM`} />
            <Spec label={model.family.replace('_', ' ').toUpperCase()} />
          </View>
        </View>
      </View>

      <View style={styles.actions}>
        <ActionRow
          status={status}
          onDownload={onDownload}
          onCancel={onCancel}
          onLoad={onLoad}
          onUnload={onUnload}
          onDelete={onDelete}
        />
      </View>

      {status.kind === 'error' ? (
        <View style={styles.errorBar}>
          <Ionicons name="alert-circle" size={14} color={colors.danger} />
          <Text style={styles.errorText}>{status.message}</Text>
        </View>
      ) : null}
    </View>
  );
}

interface ActionRowProps {
  status: Status;
  onDownload: () => void;
  onCancel: () => void;
  onLoad: () => void;
  onUnload: () => void;
  onDelete: () => void;
}

function ActionRow({
  status,
  onDownload,
  onCancel,
  onLoad,
  onUnload,
  onDelete,
}: ActionRowProps) {
  if (status.kind === 'idle' || status.kind === 'error') {
    return <PrimaryBtn icon="cloud-download-outline" label="Download" onPress={onDownload} />;
  }
  if (status.kind === 'downloading') {
    const percent = Math.round(status.progress.fraction * 100);
    return (
      <View style={{ gap: spacing.sm }}>
        <View style={styles.progressTrack}>
          <View style={[styles.progressFill, { width: `${percent}%` }]} />
        </View>
        <View style={styles.row}>
          <Text style={[typography.smallMedium, { color: colors.text, flex: 1 }]}>
            {percent}% — {(status.progress.bytesWritten / 1_000_000).toFixed(0)}/
            {(status.progress.totalBytes / 1_000_000).toFixed(0)} MB
          </Text>
          <SecondaryBtn label="Cancel" onPress={onCancel} />
        </View>
      </View>
    );
  }
  if (status.kind === 'downloaded') {
    return (
      <View style={styles.row}>
        <View style={{ flex: 1 }}>
          <PrimaryBtn icon="play" label="Load" onPress={onLoad} />
        </View>
        <SecondaryBtn label="Delete" onPress={onDelete} />
      </View>
    );
  }
  if (status.kind === 'loading') {
    return (
      <View style={styles.row}>
        <ActivityIndicator color={colors.primary} />
        <Text style={[typography.smallMedium, { color: colors.text }]}>
          Loading into memory…
        </Text>
      </View>
    );
  }
  if (status.kind === 'loaded') {
    return (
      <View style={styles.row}>
        <SecondaryBtn label="Unload" onPress={onUnload} />
        <SecondaryBtn label="Delete" onPress={onDelete} />
      </View>
    );
  }
  return null;
}

function Spec({ label }: { label: string }) {
  return (
    <View style={styles.spec}>
      <Text style={styles.specText}>{label}</Text>
    </View>
  );
}

function PrimaryBtn({
  icon,
  label,
  onPress,
}: {
  icon: React.ComponentProps<typeof Ionicons>['name'];
  label: string;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [styles.primaryBtn, { opacity: pressed ? 0.85 : 1 }]}
    >
      <Ionicons name={icon} size={16} color={colors.primaryText} />
      <Text style={styles.primaryBtnLabel}>{label}</Text>
    </Pressable>
  );
}

function SecondaryBtn({ label, onPress }: { label: string; onPress: () => void }) {
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [styles.secondaryBtn, { opacity: pressed ? 0.6 : 1 }]}
    >
      <Text style={styles.secondaryBtnLabel}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  content: {
    paddingHorizontal: spacing.xl,
    paddingTop: spacing.md,
    paddingBottom: spacing.xxxl,
    gap: spacing.lg,
  },
  hero: { gap: spacing.xs, marginBottom: spacing.sm },
  muted: { color: colors.textMuted },
  objective: { color: colors.text, marginTop: spacing.sm },

  card: {
    backgroundColor: colors.card,
    borderRadius: radius.lg,
    padding: spacing.lg,
    borderWidth: 1,
    borderColor: colors.borderMuted,
    gap: spacing.md,
  },
  cardActive: {
    borderColor: colors.success,
    backgroundColor: colors.successSoft,
  },
  cardHead: { flexDirection: 'row', alignItems: 'flex-start', gap: spacing.md },
  titleRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
    flexWrap: 'wrap',
  },

  specs: { flexDirection: 'row', gap: spacing.xs, marginTop: spacing.xs, flexWrap: 'wrap' },
  spec: {
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: radius.sm,
    backgroundColor: colors.bgMuted,
  },
  specText: { fontSize: 11, fontFamily: fonts.uiSemibold, color: colors.text },

  loadedPill: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: radius.pill,
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.success,
  },
  loadedDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: colors.success },
  loadedPillText: {
    fontSize: 10,
    fontFamily: fonts.uiBold,
    color: colors.success,
    letterSpacing: 0.6,
  },

  actions: { gap: spacing.sm },
  row: { flexDirection: 'row', alignItems: 'center', gap: spacing.sm },

  primaryBtn: {
    backgroundColor: colors.primary,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.sm,
    paddingVertical: spacing.sm + 2,
    paddingHorizontal: spacing.lg,
    borderRadius: radius.pill,
    minHeight: 44,
  },
  primaryBtnLabel: { color: colors.primaryText, fontFamily: fonts.uiSemibold, fontSize: 14 },

  secondaryBtn: {
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm + 2,
    minHeight: 44,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.card,
    alignItems: 'center',
    justifyContent: 'center',
  },
  secondaryBtnLabel: { color: colors.text, fontFamily: fonts.uiMedium, fontSize: 13 },

  progressTrack: {
    height: 6,
    backgroundColor: colors.bgMuted,
    borderRadius: 3,
    overflow: 'hidden',
  },
  progressFill: { height: '100%', backgroundColor: colors.primary, borderRadius: 3 },

  errorBar: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    backgroundColor: colors.dangerSoft,
    paddingHorizontal: spacing.sm,
    paddingVertical: 6,
    borderRadius: radius.sm,
  },
  errorText: { color: colors.danger, fontSize: 12, flex: 1 },
});
