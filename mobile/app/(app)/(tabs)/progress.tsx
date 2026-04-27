import { useRouter } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { useQuery } from '@tanstack/react-query';

import { listProgress } from '@/api/curriculum';
import { extractErrorMessage } from '@/api/client';
import { Screen } from '@/components/Screen';
import { colors, spacing, typography } from '@/theme';
import type { ProgressRow } from '@/api/types';

const MASTERY_LABEL: Record<string, string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  mastered: 'Mastered',
};

const MASTERY_COLOR: Record<string, string> = {
  not_started: colors.textMuted,
  in_progress: colors.warning,
  mastered: colors.success,
};

export default function ProgressScreen() {
  const router = useRouter();
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
      <FlatList
        data={rows ?? []}
        keyExtractor={(r) => String(r.lesson_id)}
        contentContainerStyle={styles.list}
        refreshControl={
          <RefreshControl refreshing={isRefetching} onRefresh={() => refetch()} tintColor={colors.text} />
        }
        ItemSeparatorComponent={() => <View style={styles.divider} />}
        ListHeaderComponent={
          <View style={styles.headerWrap}>
            <Text style={[typography.caption, styles.muted]}>YOUR LEARNING</Text>
            <Text style={typography.largeTitle}>Progress</Text>
            <Text style={[typography.body, styles.subtitle]}>
              Tap any lesson to jump back in.
            </Text>
            <View style={styles.sectionHeader}>
              <Text style={[typography.caption, styles.muted]}>RECENT LESSONS</Text>
            </View>
          </View>
        }
        ListEmptyComponent={
          isLoading ? (
            <View style={styles.center}>
              <ActivityIndicator color={colors.primary} />
            </View>
          ) : error ? (
            <Text style={[typography.small, styles.error]}>
              {extractErrorMessage(error, 'Could not load progress')}
            </Text>
          ) : (
            <View style={styles.empty}>
              <Text style={[typography.body, styles.muted]}>
                Complete a lesson to see progress here.
              </Text>
            </View>
          )
        }
        renderItem={({ item }) => (
          <ProgressRowItem
            row={item}
            onPress={() => router.push(`/(app)/lessons/${item.lesson_id}`)}
          />
        )}
      />
    </Screen>
  );
}

interface RowProps {
  row: ProgressRow;
  onPress: () => void;
}

function ProgressRowItem({ row, onPress }: RowProps) {
  const masteryColor = MASTERY_COLOR[row.mastery_level] ?? colors.textMuted;
  const score = row.best_score != null ? Math.round(row.best_score * 100) : null;
  return (
    <Pressable
      onPress={onPress}
      style={(state) => {
        const hovered = (state as { hovered?: boolean }).hovered;
        return [
          styles.row,
          hovered && styles.rowHover,
          state.pressed && styles.rowPressed,
        ];
      }}
    >
      <View style={{ flex: 1, gap: 4 }}>
        <Text style={typography.h3} numberOfLines={2}>
          {row.lesson_title}
        </Text>
        <View style={styles.metaRow}>
          <View style={[styles.dot, { backgroundColor: masteryColor }]} />
          <Text style={[typography.smallMedium, { color: colors.text }]}>
            {MASTERY_LABEL[row.mastery_level] ?? row.mastery_level}
          </Text>
          {score != null ? (
            <>
              <Text style={[typography.small, styles.dotSep]}>·</Text>
              <Text style={[typography.small, styles.muted]}>Best {score}%</Text>
            </>
          ) : null}
          {row.attempts_count > 0 ? (
            <>
              <Text style={[typography.small, styles.dotSep]}>·</Text>
              <Text style={[typography.small, styles.muted]}>
                {row.attempts_count} {row.attempts_count === 1 ? 'attempt' : 'attempts'}
              </Text>
            </>
          ) : null}
        </View>
      </View>
      <Ionicons name="chevron-forward" size={18} color={colors.textSubtle} />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  headerWrap: {
    paddingHorizontal: spacing.xl,
    paddingTop: spacing.lg,
    gap: spacing.xs,
  },
  muted: { color: colors.textMuted },
  subtitle: { color: colors.textMuted, marginTop: spacing.xs },
  sectionHeader: { marginTop: spacing.xxl, marginBottom: spacing.sm },
  list: { paddingBottom: spacing.xxxl },
  divider: {
    height: 1,
    backgroundColor: colors.borderMuted,
    marginHorizontal: spacing.xl,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.md,
    paddingHorizontal: spacing.xl,
    paddingVertical: spacing.md + 4,
  },
  rowHover: { backgroundColor: colors.bgMuted },
  rowPressed: { opacity: 0.6 },
  metaRow: { flexDirection: 'row', alignItems: 'center', gap: spacing.xs },
  dot: { width: 8, height: 8, borderRadius: 4 },
  dotSep: { color: colors.textSubtle },
  empty: { alignItems: 'center', paddingTop: spacing.xl },
  center: { paddingTop: spacing.xl, alignItems: 'center' },
  error: {
    color: colors.danger,
    paddingHorizontal: spacing.xl,
    marginTop: spacing.lg,
  },
});
