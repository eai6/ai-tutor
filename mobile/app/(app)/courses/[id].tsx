import { useLocalSearchParams, useRouter, Stack } from 'expo-router';
import { ActivityIndicator, FlatList, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { useQuery } from '@tanstack/react-query';

import { listLessons } from '@/api/curriculum';
import { extractErrorMessage } from '@/api/client';
import { Card } from '@/components/Card';
import { Screen } from '@/components/Screen';
import { colors, spacing, typography } from '@/theme';

export default function CourseLessonsScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const courseId = Number(id);
  const router = useRouter();

  const {
    data: lessons,
    isLoading,
    isRefetching,
    refetch,
    error,
  } = useQuery({
    queryKey: ['lessons', { courseId }],
    queryFn: () => listLessons(courseId),
    enabled: Number.isFinite(courseId),
  });

  return (
    <Screen scroll={false}>
      <Stack.Screen options={{ headerShown: true, title: 'Lessons' }} />
      {isLoading ? (
        <View style={styles.center}>
          <ActivityIndicator color={colors.primary} />
        </View>
      ) : error ? (
        <View style={styles.center}>
          <Text style={styles.error}>{extractErrorMessage(error, 'Could not load lessons')}</Text>
        </View>
      ) : (
        <FlatList
          data={lessons}
          keyExtractor={(l) => String(l.id)}
          contentContainerStyle={styles.list}
          refreshControl={
            <RefreshControl refreshing={isRefetching} onRefresh={() => refetch()} />
          }
          ListEmptyComponent={
            <View style={styles.center}>
              <Text style={[typography.body, { color: colors.textMuted }]}>
                No lessons published yet.
              </Text>
            </View>
          }
          renderItem={({ item }) => (
            <Card onPress={() => router.push(`/(app)/lessons/${item.id}`)} style={styles.card}>
              <Text style={[typography.caption, styles.unit]}>{item.unit_title}</Text>
              <Text style={typography.h3}>{item.title}</Text>
              {item.objective ? (
                <Text style={[typography.small, styles.obj]} numberOfLines={2}>
                  {item.objective}
                </Text>
              ) : null}
            </Card>
          )}
        />
      )}
    </Screen>
  );
}

const styles = StyleSheet.create({
  list: { padding: spacing.lg, gap: spacing.md },
  card: { gap: spacing.xs },
  unit: { color: colors.textMuted, textTransform: 'uppercase', letterSpacing: 0.6 },
  obj: { color: colors.textMuted, marginTop: spacing.xs },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: spacing.xl },
  error: { color: colors.danger },
});
