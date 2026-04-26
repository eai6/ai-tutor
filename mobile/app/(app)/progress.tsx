import { ActivityIndicator, FlatList, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { useQuery } from '@tanstack/react-query';

import { listProgress } from '@/api/curriculum';
import { extractErrorMessage } from '@/api/client';
import { Card } from '@/components/Card';
import { Screen } from '@/components/Screen';
import { colors, spacing, typography } from '@/theme';

const MASTERY_LABEL: Record<string, string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  mastered: 'Mastered',
};

export default function ProgressScreen() {
  const {
    data: rows,
    isLoading,
    isRefetching,
    refetch,
    error,
  } = useQuery({
    queryKey: ['progress'],
    queryFn: listProgress,
  });

  return (
    <Screen scroll={false}>
      <View style={styles.header}>
        <Text style={typography.h2}>Progress</Text>
      </View>
      {isLoading ? (
        <View style={styles.center}>
          <ActivityIndicator color={colors.primary} />
        </View>
      ) : error ? (
        <View style={styles.center}>
          <Text style={styles.error}>{extractErrorMessage(error, 'Could not load progress')}</Text>
        </View>
      ) : (
        <FlatList
          data={rows}
          keyExtractor={(r) => String(r.lesson_id)}
          contentContainerStyle={styles.list}
          refreshControl={
            <RefreshControl refreshing={isRefetching} onRefresh={() => refetch()} />
          }
          ListEmptyComponent={
            <View style={styles.center}>
              <Text style={[typography.body, { color: colors.textMuted }]}>
                Complete a lesson to see progress here.
              </Text>
            </View>
          }
          renderItem={({ item }) => (
            <Card style={styles.card}>
              <Text style={typography.h3}>{item.lesson_title}</Text>
              <Text style={[typography.small, styles.muted]}>
                {MASTERY_LABEL[item.mastery_level] ?? item.mastery_level}
                {item.best_score != null
                  ? ` · Best ${Math.round(item.best_score * 100)}%`
                  : ''}
              </Text>
            </Card>
          )}
        />
      )}
    </Screen>
  );
}

const styles = StyleSheet.create({
  header: { paddingHorizontal: spacing.lg, paddingTop: spacing.lg },
  list: { padding: spacing.lg, gap: spacing.md },
  card: { gap: spacing.xs },
  muted: { color: colors.textMuted },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: spacing.xl },
  error: { color: colors.danger },
});
