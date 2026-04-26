import { useMemo, useState } from 'react';
import { Stack, useLocalSearchParams } from 'expo-router';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useQuery } from '@tanstack/react-query';

import type { ExitTicketQuestion } from '@/api/offline-pack';
import { Button } from '@/components/Button';
import { Card } from '@/components/Card';
import { Screen } from '@/components/Screen';
import { loadPack } from '@/db/queries/lesson-packs';
import { colors, radius, spacing, typography } from '@/theme';

const OPTION_KEYS = ['A', 'B', 'C', 'D'] as const;
type OptionKey = (typeof OPTION_KEYS)[number];

interface MCQResult {
  questionId: number;
  correct: boolean;
  studentAnswer: OptionKey | null;
  correctAnswer: string;
}

export default function LessonReviewScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const lessonId = Number(id);

  const packQ = useQuery({
    queryKey: ['pack-local', lessonId],
    queryFn: () => loadPack(lessonId),
    enabled: Number.isFinite(lessonId),
  });

  const [mode, setMode] = useState<'read' | 'ticket' | 'results'>('read');
  const [answers, setAnswers] = useState<Record<number, OptionKey>>({});
  const [results, setResults] = useState<MCQResult[]>([]);

  const pack = packQ.data;
  const mcqs = useMemo<ExitTicketQuestion[]>(
    () =>
      pack?.exit_ticket?.questions?.filter(
        (q) => q.question_type === 'multiple_choice' || q.question_type === 'true_false',
      ) ?? [],
    [pack],
  );

  function gradeMcqs() {
    const graded: MCQResult[] = mcqs.map((q) => {
      const studentAnswer = answers[q.id] ?? null;
      const correctRaw = (q.correct_answer || '').trim().toUpperCase();
      const correctLetter = correctRaw.charAt(0) as OptionKey | '';
      const correct = !!studentAnswer && studentAnswer === correctLetter;
      return {
        questionId: q.id,
        correct,
        studentAnswer,
        correctAnswer: correctRaw,
      };
    });
    setResults(graded);
    setMode('results');
  }

  if (packQ.isLoading) {
    return (
      <Screen scroll={false}>
        <View style={styles.center}>
          <ActivityIndicator color={colors.primary} />
        </View>
      </Screen>
    );
  }

  if (!pack) {
    return (
      <Screen scroll={false}>
        <Stack.Screen options={{ headerShown: true, title: 'Review' }} />
        <View style={styles.center}>
          <Text style={[typography.body, styles.muted]}>
            No offline pack saved. Go back and download first.
          </Text>
        </View>
      </Screen>
    );
  }

  const lessonTitle = (pack.steps[0] as { lesson_title?: string } | undefined)?.lesson_title;

  return (
    <Screen scroll={false}>
      <Stack.Screen options={{ headerShown: true, title: lessonTitle ?? 'Review' }} />
      <ScrollView contentContainerStyle={styles.content}>
        {mode === 'read' ? (
          <ReadMode
            steps={pack.steps}
            onStartTicket={mcqs.length > 0 ? () => setMode('ticket') : undefined}
          />
        ) : mode === 'ticket' ? (
          <TicketMode
            questions={mcqs}
            answers={answers}
            onSelect={(qid, k) => setAnswers((prev) => ({ ...prev, [qid]: k }))}
            onSubmit={gradeMcqs}
            onBack={() => setMode('read')}
          />
        ) : (
          <ResultsMode
            questions={mcqs}
            results={results}
            onRetry={() => {
              setAnswers({});
              setResults([]);
              setMode('ticket');
            }}
            onClose={() => setMode('read')}
          />
        )}
      </ScrollView>
    </Screen>
  );
}

interface ReadProps {
  steps: NonNullable<ReturnType<typeof loadPack> extends Promise<infer T> ? T : never>['steps'];
  onStartTicket?: () => void;
}

function ReadMode({ steps, onStartTicket }: ReadProps) {
  return (
    <>
      {steps.length === 0 ? (
        <Text style={[typography.body, styles.muted]}>No steps in this pack.</Text>
      ) : (
        steps.map((s, idx) => (
          <Card key={s.id} style={styles.stepCard}>
            <Text style={[typography.caption, styles.muted]}>
              Step {idx + 1} · {s.phase || s.step_type}
            </Text>
            {s.teacher_script ? (
              <Text style={[typography.body, styles.script]}>{s.teacher_script}</Text>
            ) : null}
            {s.question ? (
              <Text style={[typography.small, styles.question]}>Q: {s.question}</Text>
            ) : null}
          </Card>
        ))
      )}
      {onStartTicket ? (
        <Button title="Take exit ticket (offline)" onPress={onStartTicket} />
      ) : (
        <Text style={[typography.small, styles.muted]}>
          No multiple-choice exit ticket questions in this pack.
        </Text>
      )}
    </>
  );
}

interface TicketProps {
  questions: ExitTicketQuestion[];
  answers: Record<number, OptionKey>;
  onSelect: (qid: number, key: OptionKey) => void;
  onSubmit: () => void;
  onBack: () => void;
}

function TicketMode({ questions, answers, onSelect, onSubmit, onBack }: TicketProps) {
  const allAnswered = questions.every((q) => answers[q.id]);
  return (
    <>
      <Text style={typography.h2}>Exit ticket</Text>
      <Text style={[typography.small, styles.muted]}>Pick one answer per question.</Text>

      {questions.map((q, i) => (
        <Card key={q.id} style={styles.stepCard}>
          <Text style={typography.h3}>
            {i + 1}. {q.question_text}
          </Text>
          {OPTION_KEYS.map((k) => {
            const text = q[`option_${k.toLowerCase()}` as 'option_a' | 'option_b' | 'option_c' | 'option_d'];
            if (!text) return null;
            const selected = answers[q.id] === k;
            return (
              <Pressable
                key={k}
                onPress={() => onSelect(q.id, k)}
                style={[styles.option, selected ? styles.optionSelected : null]}
              >
                <Text style={styles.optionLetter}>{k}</Text>
                <Text style={styles.optionText}>{text}</Text>
              </Pressable>
            );
          })}
        </Card>
      ))}

      <Button title="Submit answers" onPress={onSubmit} disabled={!allAnswered} />
      <Button title="Back to lesson" variant="secondary" onPress={onBack} />
    </>
  );
}

interface ResultsProps {
  questions: ExitTicketQuestion[];
  results: MCQResult[];
  onRetry: () => void;
  onClose: () => void;
}

function ResultsMode({ questions, results, onRetry, onClose }: ResultsProps) {
  const correctCount = results.filter((r) => r.correct).length;
  const total = results.length;
  const score = total > 0 ? Math.round((correctCount / total) * 100) : 0;

  return (
    <>
      <Card style={styles.scoreCard}>
        <Text style={typography.h2}>Score: {score}%</Text>
        <Text style={[typography.small, styles.muted]}>
          {correctCount} of {total} correct · graded on this device
        </Text>
        <Text style={[typography.caption, styles.muted]}>
          Results sync to your teacher when you reconnect.
        </Text>
      </Card>

      {questions.map((q, i) => {
        const r = results.find((x) => x.questionId === q.id);
        if (!r) return null;
        return (
          <Card key={q.id} style={styles.stepCard}>
            <Text style={[typography.caption, r.correct ? styles.success : styles.dangerText]}>
              {r.correct ? '✓ Correct' : '✗ Incorrect'}
            </Text>
            <Text style={typography.h3}>
              {i + 1}. {q.question_text}
            </Text>
            <Text style={[typography.small, styles.muted]}>
              Your answer: {r.studentAnswer ?? '—'} · Correct: {r.correctAnswer}
            </Text>
            {q.explanation ? (
              <Text style={[typography.small, styles.explanation]}>{q.explanation}</Text>
            ) : null}
          </Card>
        );
      })}

      <Button title="Try again" onPress={onRetry} />
      <Button title="Back to lesson" variant="secondary" onPress={onClose} />
    </>
  );
}

const styles = StyleSheet.create({
  content: { padding: spacing.lg, gap: spacing.md, paddingBottom: spacing.xxl },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: spacing.xl },
  muted: { color: colors.textMuted },
  stepCard: { gap: spacing.xs },
  script: { marginTop: spacing.xs },
  question: { marginTop: spacing.xs, color: colors.text, fontStyle: 'italic' },
  option: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.md,
    padding: spacing.md,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    marginTop: spacing.xs,
  },
  optionSelected: {
    borderColor: colors.primary,
    backgroundColor: '#eff6ff',
  },
  optionLetter: {
    fontSize: 16,
    fontWeight: '700',
    width: 24,
    textAlign: 'center',
    color: colors.primary,
  },
  optionText: { flex: 1, fontSize: 14, color: colors.text },
  scoreCard: { gap: spacing.xs, alignItems: 'center' },
  success: { color: colors.success, fontWeight: '700' },
  dangerText: { color: colors.danger, fontWeight: '700' },
  explanation: { marginTop: spacing.xs, color: colors.textMuted, fontStyle: 'italic' },
});
