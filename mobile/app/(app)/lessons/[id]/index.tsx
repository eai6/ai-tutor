import { useState } from 'react';
import { Stack, useLocalSearchParams, useRouter } from 'expo-router';
import { ActivityIndicator, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { extractErrorMessage } from '@/api/client';
import { getLesson, listLessonSteps } from '@/api/curriculum';
import { fetchOfflinePack } from '@/api/offline-pack';
import { startSession } from '@/api/sessions';
import { Button } from '@/components/Button';
import { Card } from '@/components/Card';
import { Screen } from '@/components/Screen';
import { loadPack, savePack } from '@/db/queries/lesson-packs';
import { colors, spacing, typography } from '@/theme';

export default function LessonDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const lessonId = Number(id);
  const router = useRouter();
  const qc = useQueryClient();
  const [startError, setStartError] = useState<string | null>(null);

  const lessonQ = useQuery({
    queryKey: ['lesson', lessonId],
    queryFn: () => getLesson(lessonId),
    enabled: Number.isFinite(lessonId),
  });

  const stepsQ = useQuery({
    queryKey: ['lesson-steps', lessonId],
    queryFn: () => listLessonSteps(lessonId),
    enabled: Number.isFinite(lessonId),
  });

  const packQ = useQuery({
    queryKey: ['pack-local', lessonId],
    queryFn: () => loadPack(lessonId),
    enabled: Number.isFinite(lessonId),
  });

  const downloadM = useMutation({
    mutationFn: async (refresh: boolean) => {
      const pack = await fetchOfflinePack(lessonId, refresh);
      await savePack(pack);
      return pack;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pack-local', lessonId] });
    },
  });

  const startM = useMutation({
    mutationFn: () => startSession(lessonId),
    onSuccess: (resp) => {
      setStartError(null);
      router.push(`/(app)/tutor/${resp.session_id}`);
    },
    onError: (err) => {
      setStartError(extractErrorMessage(err, 'Could not start session'));
    },
  });

  if (lessonQ.isLoading) {
    return (
      <Screen scroll={false}>
        <View style={styles.center}>
          <ActivityIndicator color={colors.primary} />
        </View>
      </Screen>
    );
  }

  if (lessonQ.error || !lessonQ.data) {
    return (
      <Screen scroll={false}>
        <View style={styles.center}>
          <Text style={styles.error}>
            {extractErrorMessage(lessonQ.error, 'Could not load lesson')}
          </Text>
        </View>
      </Screen>
    );
  }

  const lesson = lessonQ.data;
  const steps = stepsQ.data ?? [];
  const pack = packQ.data;
  const isDownloaded = !!pack;
  const downloadError = downloadM.error
    ? extractErrorMessage(downloadM.error, 'Download failed')
    : null;

  return (
    <Screen scroll={false}>
      <Stack.Screen options={{ headerShown: true, title: lesson.unit_title }} />
      <ScrollView contentContainerStyle={styles.content}>
        <View style={styles.header}>
          <Text style={[typography.caption, styles.muted]}>{lesson.course_title}</Text>
          <Text style={typography.h1}>{lesson.title}</Text>
          {lesson.objective ? (
            <Text style={[typography.body, styles.muted]}>{lesson.objective}</Text>
          ) : null}
        </View>

        <Button
          title={startM.isPending ? 'Starting…' : 'Start tutoring session'}
          onPress={() => startM.mutate()}
          loading={startM.isPending}
        />
        {startError ? <Text style={styles.error}>{startError}</Text> : null}

        <Card style={styles.packCard}>
          <Text style={typography.h3}>Offline content</Text>
          {isDownloaded ? (
            <>
              <Text style={[typography.small, styles.muted]}>
                Pack v{pack.version} · saved{' '}
                {pack.downloaded_at ? new Date(pack.downloaded_at).toLocaleDateString() : ''}
              </Text>
              <Button
                title="Review offline"
                variant="secondary"
                onPress={() => router.push(`/(app)/lessons/${lessonId}/review`)}
              />
              <Button
                title={downloadM.isPending ? 'Refreshing…' : 'Refresh pack'}
                variant="secondary"
                loading={downloadM.isPending}
                onPress={() => downloadM.mutate(true)}
              />
            </>
          ) : (
            <>
              <Text style={[typography.small, styles.muted]}>
                Download to read this lesson and take the exit ticket without internet.
              </Text>
              <Button
                title={downloadM.isPending ? 'Downloading…' : 'Download for offline'}
                onPress={() => downloadM.mutate(false)}
                loading={downloadM.isPending}
              />
            </>
          )}
          {downloadError ? <Text style={styles.error}>{downloadError}</Text> : null}
        </Card>

        <View style={styles.section}>
          <Text style={typography.h3}>Lesson outline</Text>
          {stepsQ.isLoading ? (
            <ActivityIndicator color={colors.primary} />
          ) : steps.length === 0 ? (
            <Text style={[typography.small, styles.muted]}>No steps published yet.</Text>
          ) : (
            steps.map((s, idx) => (
              <Card key={s.id} style={styles.stepCard}>
                <Text style={[typography.caption, styles.muted]}>
                  Step {idx + 1} · {s.phase || s.step_type}
                </Text>
                {s.teacher_script ? (
                  <Text style={[typography.body, styles.script]} numberOfLines={4}>
                    {s.teacher_script}
                  </Text>
                ) : null}
                {s.question ? (
                  <Text style={[typography.small, styles.question]} numberOfLines={3}>
                    Q: {s.question}
                  </Text>
                ) : null}
              </Card>
            ))
          )}
        </View>
      </ScrollView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  content: { padding: spacing.lg, gap: spacing.lg, paddingBottom: spacing.xxl },
  header: { gap: spacing.xs },
  muted: { color: colors.textMuted },
  packCard: { gap: spacing.sm },
  section: { gap: spacing.md, marginTop: spacing.md },
  stepCard: { gap: spacing.xs },
  script: { marginTop: spacing.xs },
  question: { marginTop: spacing.xs, color: colors.text, fontStyle: 'italic' },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  error: { color: colors.danger },
});
