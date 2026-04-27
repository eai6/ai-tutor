import { useEffect, useRef, useState } from 'react';
import { Stack, useLocalSearchParams } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { KeyboardAvoidingView } from 'react-native-keyboard-controller';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';
import { useMutation } from '@tanstack/react-query';

import { extractErrorMessage } from '@/api/client';
import { fetchOfflinePack } from '@/api/offline-pack';
import {
  listSessionTurns,
  respond as cloudRespond,
  startSession as cloudStartSession,
  type SessionTurn,
} from '@/api/sessions';
import { ChatBubble } from '@/components/ChatBubble';
import { TypingIndicator } from '@/components/TypingIndicator';
import { MODEL_CATALOG, pickDefaultModel } from '@/inference/catalog';
import { isModelDownloaded } from '@/inference/download';
import { llamaClient } from '@/inference/llama-client';
import { loadPack, savePack } from '@/db/queries/lesson-packs';
import {
  appendTurn as persistTurn,
  loadSession,
  loadTurns,
  offlineSessionId,
  saveSession,
} from '@/db/queries/offline-sessions';
import { TutorRunner } from '@/state-machine/runner';
import type { ConversationTurn } from '@/state-machine/types';
import { useAuthStore } from '@/stores/auth-store';
import { useModelStore } from '@/stores/model-store';
import { colors, fonts, radius, spacing, typography } from '@/theme';

type Mode = 'local' | 'cloud';

interface DisplayTurn {
  key: string;
  role: 'student' | 'tutor' | 'system';
  content: string;
}

function localTurnToDisplay(t: ConversationTurn): DisplayTurn {
  return { key: t.client_uuid, role: t.role, content: t.content };
}

function cloudTurnToDisplay(t: SessionTurn): DisplayTurn {
  return { key: `srv-${t.id}`, role: t.role, content: t.content };
}

export default function LessonChatScreen() {
  const { lessonId } = useLocalSearchParams<{ lessonId: string }>();
  const lessonIdNum = Number(lessonId);
  const insets = useSafeAreaInsets();
  const listRef = useRef<FlatList<DisplayTurn>>(null);
  const userId = useAuthStore((s) => s.user?.id ?? 0);
  const setModelStatus = useModelStore((s) => s.setStatus);
  const setLoadedModelId = useModelStore((s) => s.setLoadedModelId);

  const [mode, setMode] = useState<Mode | null>(null);
  const [bootStage, setBootStage] = useState<string>('Starting…');
  const [bootError, setBootError] = useState<string | null>(null);
  const [runner, setRunner] = useState<TutorRunner | null>(null);
  const [cloudSessionId, setCloudSessionId] = useState<number | null>(null);
  const [turns, setTurns] = useState<DisplayTurn[]>([]);

  const [draft, setDraft] = useState('');
  const [waiting, setWaiting] = useState(false);
  const [respondError, setRespondError] = useState<string | null>(null);

  // Reset trigger — bumping this re-runs the boot effect (used by the
  // mode toggle to restart the conversation in the other backend).
  const [bootGeneration, setBootGeneration] = useState(0);
  const [forcedMode, setForcedMode] = useState<Mode | null>(null);

  useEffect(() => {
    let cancelled = false;
    void boot();
    return () => {
      cancelled = true;
    };

    async function boot() {
      setBootError(null);
      setRunner(null);
      setCloudSessionId(null);
      setTurns([]);
      setWaiting(false);

      try {
        // Find which catalog models are on disk so we can prefer
        // local over cloud when something's available.
        const downloadedIds: string[] = [];
        for (const m of MODEL_CATALOG) {
          if (await isModelDownloaded(m)) downloadedIds.push(m.id);
        }
        const wantsLocal =
          forcedMode === 'local' ||
          (forcedMode == null && (llamaClient.isReady() || downloadedIds.length > 0));
        if (cancelled) return;

        if (wantsLocal) {
          await bootLocal(downloadedIds);
        } else {
          await bootCloud();
        }
      } catch (e) {
        if (!cancelled) {
          setBootError(e instanceof Error ? e.message : 'Failed to start tutor');
        }
      }
    }

    async function bootLocal(downloadedIds: string[]) {
      // 1. Auto-load model if downloaded but not active. Prefer the
      // already-loaded model; otherwise pick the largest-downloaded
      // one (assumes the user wouldn't keep it on disk if not wanted).
      if (!llamaClient.isReady()) {
        const target = pickDefaultModel(downloadedIds);
        if (!target) {
          throw new Error(
            'No on-device model downloaded. Open Settings → AI Model to download one, then come back.',
          );
        }
        setBootStage(`Loading ${target.display_name} into memory…`);
        setModelStatus('loading');
        await llamaClient.load(target);
        setLoadedModelId(target.id);
        setModelStatus('loaded');
      }
      if (cancelled) return;

      // 2. Get the lesson pack — fetch + save if missing locally.
      setBootStage('Loading lesson pack…');
      let pack = await loadPack(lessonIdNum);
      if (!pack) {
        const fetched = await fetchOfflinePack(lessonIdNum, false);
        await savePack(fetched);
        pack = await loadPack(lessonIdNum);
        if (!pack) throw new Error('Could not load the lesson pack.');
      }
      if (cancelled) return;

      // 3. Restore prior offline session if any.
      const sessionId = offlineSessionId(lessonIdNum, userId);
      const stored = await loadSession(lessonIdNum);
      const initialTurns = stored ? await loadTurns(stored.id) : [];

      const fakePack = {
        lesson_id: pack.lesson_id,
        pack_version: pack.version,
        created_at: pack.downloaded_at.toISOString(),
        policy: pack.policy,
        content: {
          lesson: { id: pack.lesson_id } as never,
          steps: pack.steps,
          exit_ticket: pack.exit_ticket,
        },
        media_manifest: pack.media_manifest ?? [],
        student_progress: null,
      } as ConstructorParameters<typeof TutorRunner>[0]['pack'];

      const r = new TutorRunner({
        pack: fakePack,
        client: llamaClient,
        initialSnapshot: stored?.snapshot,
        initialTurns,
      });
      r.onTurnAppended((t) => {
        void persistTurn(sessionId, t);
      });

      if (cancelled) return;
      setRunner(r);
      setMode('local');
      setTurns(initialTurns.map(localTurnToDisplay));
      setBootStage('Ready');

      if (initialTurns.length === 0) {
        const startedAt = new Date();
        await saveSession({
          id: sessionId,
          lessonId: lessonIdNum,
          studentId: userId,
          packVersion: pack.version,
          status: 'active',
          snapshot: r.getSnapshot(),
          startedAt,
          endedAt: null,
        });
        setBootStage('Tutor is opening the lesson…');
        await r.start();
        if (cancelled) return;
        setTurns(r.getTurns().map(localTurnToDisplay));
        await saveSession({
          id: sessionId,
          lessonId: lessonIdNum,
          studentId: userId,
          packVersion: pack.version,
          status: 'active',
          snapshot: r.getSnapshot(),
          startedAt,
          endedAt: null,
        });
      }
    }

    async function bootCloud() {
      setBootStage('Starting cloud session…');
      const startResp = await cloudStartSession(lessonIdNum);
      if (cancelled) return;
      setCloudSessionId(startResp.session_id);
      setMode('cloud');

      const existing = await listSessionTurns(startResp.session_id);
      if (cancelled) return;
      setTurns(existing.map(cloudTurnToDisplay));
      // start_session returns the opening tutor message even when
      // resumed=true; if the turns list is empty the message will land
      // on next refresh. For simplicity we don't append manually here.
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lessonIdNum, bootGeneration, forcedMode, userId]);

  const respondLocalM = useMutation({
    mutationFn: async (text: string) => {
      if (!runner) throw new Error('Runner not initialised');
      await runner.respond(text);
      return runner.getTurns();
    },
    onMutate: () => {
      setWaiting(true);
      setRespondError(null);
    },
    onSuccess: async (newTurns) => {
      setWaiting(false);
      setTurns(newTurns.map(localTurnToDisplay));
      if (runner) {
        const snapshot = runner.getSnapshot();
        const sessionId = offlineSessionId(lessonIdNum, userId);
        await saveSession({
          id: sessionId,
          lessonId: lessonIdNum,
          studentId: userId,
          packVersion: snapshot.current_step_index,
          status: snapshot.session_state === 'completed' ? 'completed' : 'active',
          snapshot,
          startedAt: new Date(),
          endedAt: snapshot.session_state === 'completed' ? new Date() : null,
        });
      }
    },
    onError: (err) => {
      setWaiting(false);
      setRespondError(extractErrorMessage(err, 'On-device generation failed'));
    },
  });

  const respondCloudM = useMutation({
    mutationFn: (text: string) => {
      if (cloudSessionId == null) throw new Error('No cloud session');
      return cloudRespond(cloudSessionId, text);
    },
    onMutate: (text) => {
      setWaiting(true);
      setRespondError(null);
      const studentTurn: DisplayTurn = {
        key: `local-${Date.now()}`,
        role: 'student',
        content: text,
      };
      setTurns((prev) => [...prev, studentTurn]);
    },
    onSuccess: (msg) => {
      setWaiting(false);
      setTurns((prev) => [
        ...prev,
        { key: `cloud-tutor-${Date.now()}`, role: 'tutor', content: msg.message },
      ]);
    },
    onError: (err) => {
      setWaiting(false);
      setRespondError(extractErrorMessage(err, 'Tutor response failed'));
    },
  });

  useEffect(() => {
    if (turns.length === 0 && !waiting) return;
    const t = setTimeout(() => listRef.current?.scrollToEnd({ animated: true }), 60);
    return () => clearTimeout(t);
  }, [turns.length, waiting]);

  function handleSend() {
    const text = draft.trim();
    if (!text || waiting) return;
    setDraft('');
    if (mode === 'local') respondLocalM.mutate(text);
    else if (mode === 'cloud') respondCloudM.mutate(text);
  }

  function handleSwitchMode() {
    const target: Mode = mode === 'local' ? 'cloud' : 'local';
    Alert.alert(
      'Switch tutor source?',
      target === 'local'
        ? 'Restart this lesson with the on-device tutor. The current conversation will reset.'
        : 'Restart this lesson with the cloud tutor. The current conversation will reset.',
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Switch',
          style: 'destructive',
          onPress: () => {
            setForcedMode(target);
            setBootGeneration((n) => n + 1);
          },
        },
      ],
    );
  }

  const headerHeight = 56;
  const headerBadge = mode ? <ModeBadge mode={mode} onPress={handleSwitchMode} /> : null;

  if (bootError) {
    return (
      <SafeAreaView style={styles.safe} edges={['top']}>
        <Stack.Screen options={{ title: 'Tutor', headerRight: () => headerBadge }} />
        <View style={styles.center}>
          <View style={styles.emptyIconWrap}>
            <Ionicons name="alert-circle-outline" size={22} color={colors.danger} />
          </View>
          <Text style={[typography.h3, styles.centerText]}>{bootError}</Text>
        </View>
      </SafeAreaView>
    );
  }

  if (mode == null) {
    return (
      <SafeAreaView style={styles.safe} edges={['top']}>
        <Stack.Screen options={{ title: 'Tutor' }} />
        <View style={styles.center}>
          <ActivityIndicator color={colors.primary} />
          <Text style={[typography.small, styles.muted]}>{bootStage}</Text>
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <Stack.Screen options={{ title: 'Tutor', headerRight: () => headerBadge }} />
      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
        keyboardVerticalOffset={insets.top + headerHeight}
      >
        <FlatList
          ref={listRef}
          data={turns}
          keyExtractor={(t) => t.key}
          contentContainerStyle={styles.list}
          renderItem={({ item, index }) => {
            const prev = turns[index - 1];
            const showAvatar = !prev || prev.role !== item.role;
            return <ChatBubble role={item.role} content={item.content} showAvatar={showAvatar} />;
          }}
          ListFooterComponent={waiting ? <TypingIndicator /> : null}
          ListEmptyComponent={
            <View style={styles.emptyWrap}>
              <View style={styles.emptyIconWrap}>
                <Ionicons name="sparkles" size={22} color={colors.primary} />
              </View>
              <Text style={[typography.h3, { color: colors.text, textAlign: 'center' }]}>
                Say hello to your tutor
              </Text>
              <Text style={[typography.small, styles.muted, { textAlign: 'center' }]}>
                Type below to start the conversation.
              </Text>
            </View>
          }
        />
        {respondError ? (
          <View style={styles.errorBar}>
            <Ionicons name="alert-circle" size={16} color={colors.danger} />
            <Text style={styles.errorText}>{respondError}</Text>
          </View>
        ) : null}
        <View style={[styles.inputBar, { paddingBottom: spacing.md + insets.bottom }]}>
          <TextInput
            value={draft}
            onChangeText={setDraft}
            placeholder="Type your answer…"
            placeholderTextColor={colors.textMuted}
            style={styles.input}
            multiline
            editable={!waiting}
            onSubmitEditing={handleSend}
          />
          <Pressable
            onPress={handleSend}
            disabled={waiting || !draft.trim()}
            style={({ pressed }) => [
              styles.sendBtn,
              {
                backgroundColor:
                  waiting || !draft.trim() ? colors.bgMuted : colors.primary,
                opacity: pressed ? 0.85 : 1,
              },
            ]}
          >
            {waiting ? (
              <ActivityIndicator size="small" color={colors.textMuted} />
            ) : (
              <Ionicons
                name="arrow-up"
                size={20}
                color={draft.trim() ? colors.primaryText : colors.textMuted}
              />
            )}
          </Pressable>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

function ModeBadge({ mode, onPress }: { mode: Mode; onPress: () => void }) {
  const isLocal = mode === 'local';
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        styles.modeBadge,
        { opacity: pressed ? 0.6 : 1 },
        isLocal ? styles.modeBadgeLocal : styles.modeBadgeCloud,
      ]}
    >
      <Ionicons
        name={isLocal ? 'phone-portrait' : 'cloud'}
        size={12}
        color={isLocal ? colors.success : colors.primary}
      />
      <Text style={styles.modeBadgeLabel}>{isLocal ? 'On-device' : 'Cloud'}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  flex: { flex: 1 },
  list: { paddingVertical: spacing.lg, gap: spacing.xs, flexGrow: 1 },

  center: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: spacing.xl,
    gap: spacing.sm,
  },
  centerText: { textAlign: 'center', color: colors.text },
  muted: { color: colors.textMuted, marginTop: spacing.sm },

  emptyWrap: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: spacing.xl,
    gap: spacing.sm,
    minHeight: 280,
  },
  emptyIconWrap: {
    width: 44,
    height: 44,
    borderRadius: 22,
    backgroundColor: colors.primarySoft,
    alignItems: 'center',
    justifyContent: 'center',
  },

  errorBar: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
    backgroundColor: colors.dangerSoft,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
    marginHorizontal: spacing.md,
    borderRadius: radius.md,
  },
  errorText: { color: colors.danger, flex: 1, fontSize: 13 },

  inputBar: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: spacing.sm,
    padding: spacing.md,
    borderTopWidth: 1,
    borderTopColor: colors.borderMuted,
    backgroundColor: colors.bg,
  },
  input: {
    flex: 1,
    minHeight: 44,
    maxHeight: 120,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.xl,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm + 2,
    fontSize: 16,
    color: colors.text,
    backgroundColor: colors.card,
  },
  sendBtn: {
    width: 44,
    height: 44,
    borderRadius: 22,
    alignItems: 'center',
    justifyContent: 'center',
  },

  modeBadge: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: spacing.sm + 2,
    paddingVertical: 5,
    borderRadius: radius.pill,
    marginRight: spacing.md,
  },
  modeBadgeLocal: { backgroundColor: colors.successSoft },
  modeBadgeCloud: { backgroundColor: colors.primarySoft },
  modeBadgeLabel: {
    fontFamily: fonts.uiSemibold,
    fontSize: 11,
    color: colors.text,
  },
});
