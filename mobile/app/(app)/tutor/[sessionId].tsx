import { useEffect, useRef, useState } from 'react';
import { Stack, useLocalSearchParams } from 'expo-router';
import {
  ActivityIndicator,
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useMutation, useQuery } from '@tanstack/react-query';

import { extractErrorMessage } from '@/api/client';
import { listSessionTurns, respond, type SessionTurn } from '@/api/sessions';
import { ChatBubble } from '@/components/ChatBubble';
import { colors, radius, spacing, typography } from '@/theme';

interface DisplayTurn {
  key: string;
  role: 'tutor' | 'student' | 'system';
  content: string;
  pending?: boolean;
}

function turnToDisplay(t: SessionTurn): DisplayTurn {
  return { key: `srv-${t.id}`, role: t.role, content: t.content };
}

export default function TutorChatScreen() {
  const { sessionId } = useLocalSearchParams<{ sessionId: string }>();
  const sessionIdNum = Number(sessionId);
  const listRef = useRef<FlatList<DisplayTurn>>(null);

  const [draft, setDraft] = useState('');
  const [localTurns, setLocalTurns] = useState<DisplayTurn[]>([]);
  const [pendingTutor, setPendingTutor] = useState<DisplayTurn | null>(null);
  const [error, setError] = useState<string | null>(null);

  const turnsQ = useQuery({
    queryKey: ['session-turns', sessionIdNum],
    queryFn: () => listSessionTurns(sessionIdNum),
    enabled: Number.isFinite(sessionIdNum),
  });

  const respondM = useMutation({
    mutationFn: (text: string) => respond(sessionIdNum, text),
    onMutate: (text) => {
      const studentTurn: DisplayTurn = {
        key: `local-${Date.now()}`,
        role: 'student',
        content: text,
      };
      setLocalTurns((prev) => [...prev, studentTurn]);
      setPendingTutor({ key: `pending-${Date.now()}`, role: 'tutor', content: '…', pending: true });
      setError(null);
    },
    onSuccess: (msg) => {
      setPendingTutor(null);
      setLocalTurns((prev) => [
        ...prev,
        { key: `local-tutor-${Date.now()}`, role: 'tutor', content: msg.message },
      ]);
    },
    onError: (err) => {
      setPendingTutor(null);
      setError(extractErrorMessage(err, 'Tutor response failed'));
    },
  });

  const allTurns: DisplayTurn[] = [
    ...(turnsQ.data?.map(turnToDisplay) ?? []),
    ...localTurns,
    ...(pendingTutor ? [pendingTutor] : []),
  ];

  useEffect(() => {
    if (allTurns.length === 0) return;
    const t = setTimeout(() => listRef.current?.scrollToEnd({ animated: true }), 50);
    return () => clearTimeout(t);
  }, [allTurns.length]);

  function handleSend() {
    const text = draft.trim();
    if (!text || respondM.isPending) return;
    setDraft('');
    respondM.mutate(text);
  }

  if (turnsQ.isLoading) {
    return (
      <SafeAreaView style={styles.safe} edges={['top']}>
        <Stack.Screen options={{ headerShown: true, title: 'Tutor' }} />
        <View style={styles.center}>
          <ActivityIndicator color={colors.primary} />
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <Stack.Screen options={{ headerShown: true, title: 'Tutor' }} />
      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      >
        <FlatList
          ref={listRef}
          data={allTurns}
          keyExtractor={(t) => t.key}
          contentContainerStyle={styles.list}
          renderItem={({ item }) => (
            <ChatBubble role={item.role} content={item.content} pending={item.pending} />
          )}
          ListEmptyComponent={
            <View style={styles.center}>
              <Text style={[typography.body, styles.muted]}>
                No messages yet. Start the conversation below.
              </Text>
            </View>
          }
        />
        {error ? <Text style={styles.error}>{error}</Text> : null}
        <View style={styles.inputBar}>
          <TextInput
            value={draft}
            onChangeText={setDraft}
            placeholder="Type your answer…"
            placeholderTextColor={colors.textMuted}
            style={styles.input}
            multiline
            editable={!respondM.isPending}
          />
          <Pressable
            onPress={handleSend}
            disabled={respondM.isPending || !draft.trim()}
            style={({ pressed }) => [
              styles.sendBtn,
              {
                opacity: respondM.isPending || !draft.trim() ? 0.5 : pressed ? 0.85 : 1,
              },
            ]}
          >
            <Text style={styles.sendText}>{respondM.isPending ? '…' : 'Send'}</Text>
          </Pressable>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  flex: { flex: 1 },
  list: { paddingVertical: spacing.md, gap: spacing.xs },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: spacing.xl },
  muted: { color: colors.textMuted, textAlign: 'center' },
  error: {
    color: colors.danger,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.xs,
  },
  inputBar: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    gap: spacing.sm,
    padding: spacing.md,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    backgroundColor: colors.bg,
  },
  input: {
    flex: 1,
    minHeight: 44,
    maxHeight: 120,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    fontSize: 16,
    color: colors.text,
    backgroundColor: colors.bg,
  },
  sendBtn: {
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.lg,
    borderRadius: radius.md,
    backgroundColor: colors.primary,
    minHeight: 44,
    justifyContent: 'center',
  },
  sendText: { color: colors.primaryText, fontWeight: '700', fontSize: 15 },
});
