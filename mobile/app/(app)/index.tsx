import { useRouter } from 'expo-router';
import { ActivityIndicator, FlatList, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { useQuery } from '@tanstack/react-query';

import { listCourses } from '@/api/curriculum';
import { extractErrorMessage } from '@/api/client';
import { Card } from '@/components/Card';
import { Screen } from '@/components/Screen';
import { useAuthStore } from '@/stores/auth-store';
import { colors, spacing, typography } from '@/theme';

export default function HomeScreen() {
  const user = useAuthStore((s) => s.user);
  const router = useRouter();

  const {
    data: courses,
    isLoading,
    isRefetching,
    refetch,
    error,
  } = useQuery({
    queryKey: ['courses'],
    queryFn: listCourses,
  });

  return (
    <Screen scroll={false}>
      <View style={styles.header}>
        <Text style={typography.h2}>
          Hello{user?.first_name ? `, ${user.first_name}` : ''} 👋
        </Text>
        <Text style={[typography.small, { color: colors.textMuted }]}>Your courses</Text>
      </View>

      {isLoading ? (
        <View style={styles.center}>
          <ActivityIndicator color={colors.primary} />
        </View>
      ) : error ? (
        <View style={styles.center}>
          <Text style={styles.error}>{extractErrorMessage(error, 'Could not load courses')}</Text>
        </View>
      ) : (
        <FlatList
          data={courses}
          keyExtractor={(c) => String(c.id)}
          contentContainerStyle={styles.list}
          refreshControl={
            <RefreshControl refreshing={isRefetching} onRefresh={() => refetch()} />
          }
          ListEmptyComponent={
            <View style={styles.center}>
              <Text style={[typography.body, { color: colors.textMuted }]}>
                No courses available yet.
              </Text>
            </View>
          }
          renderItem={({ item }) => (
            <Card onPress={() => router.push(`/(app)/courses/${item.id}`)} style={styles.card}>
              <Text style={typography.h3}>{item.title}</Text>
              <Text style={[typography.small, { color: colors.textMuted }]}>
                Grade {item.grade_level}
                {item.subject_type ? ` · ${item.subject_type}` : ''}
              </Text>
              {item.description ? (
                <Text style={[typography.small, styles.desc]} numberOfLines={2}>
                  {item.description}
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
  header: { paddingHorizontal: spacing.lg, paddingTop: spacing.lg, gap: spacing.xs },
  list: { padding: spacing.lg, gap: spacing.md },
  card: { gap: spacing.xs },
  desc: { color: colors.textMuted, marginTop: spacing.xs },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: spacing.xl },
  error: { color: colors.danger },
});
