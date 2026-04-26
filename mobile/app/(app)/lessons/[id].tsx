import { Stack, useLocalSearchParams } from 'expo-router';
import { ActivityIndicator, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useQuery } from '@tanstack/react-query';

import { getLesson, listLessonSteps } from '@/api/curriculum';
import { extractErrorMessage } from '@/api/client';
import { Button } from '@/components/Button';
import { Card } from '@/components/Card';
import { Screen } from '@/components/Screen';
import { colors, spacing, typography } from '@/theme';

export default function LessonDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const lessonId = Number(id);

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
          title="Start tutoring session"
          onPress={() => {
            // Phase E will route to /tutor/[sessionId]; for now just stub.
            alert('Tutor chat ships in Phase E.');
          }}
        />

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
  section: { gap: spacing.md, marginTop: spacing.md },
  stepCard: { gap: spacing.xs },
  script: { marginTop: spacing.xs },
  question: { marginTop: spacing.xs, color: colors.text, fontStyle: 'italic' },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  error: { color: colors.danger },
});
