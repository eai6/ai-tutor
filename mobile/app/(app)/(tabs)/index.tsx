import { useRouter } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { useQuery } from '@tanstack/react-query';

import { listCourses } from '@/api/curriculum';
import { extractErrorMessage } from '@/api/client';
import { Screen } from '@/components/Screen';
import { SkeletonBlock } from '@/components/Skeleton';
import { useAuthStore } from '@/stores/auth-store';
import { colors, radius, spacing, typography } from '@/theme';
import type { Course } from '@/api/types';

type IoniconName = React.ComponentProps<typeof Ionicons>['name'];

export default function HomeScreen() {
  const user = useAuthStore((s) => s.user);
  const router = useRouter();
  const greeting = greetingForHour();
  const displayName = user?.first_name || user?.username || 'there';

  const {
    data: courses,
    isLoading,
    isRefetching,
    refetch,
    error,
  } = useQuery({ queryKey: ['courses'], queryFn: listCourses });

  return (
    <Screen scroll={false}>
      <FlatList
        data={courses ?? []}
        keyExtractor={(c) => String(c.id)}
        contentContainerStyle={styles.list}
        refreshControl={
          <RefreshControl refreshing={isRefetching} onRefresh={() => refetch()} tintColor={colors.text} />
        }
        ListHeaderComponent={
          <View style={styles.headerWrap}>
            <Text style={[typography.caption, styles.muted]}>{greeting}</Text>
            <Text style={typography.largeTitle}>Hi, {displayName}</Text>
            <Text style={[typography.body, styles.subtitle]}>
              Pick up where you left off, or start a new lesson.
            </Text>
            <View style={styles.sectionHeader}>
              <Text style={[typography.caption, styles.muted]}>YOUR COURSES</Text>
            </View>
          </View>
        }
        ItemSeparatorComponent={() => <View style={styles.divider} />}
        ListEmptyComponent={
          isLoading ? (
            <View style={{ paddingHorizontal: spacing.xl }}>
              <SkeletonBlock height={20} width="60%" />
              <SkeletonBlock height={14} width="40%" style={{ marginTop: spacing.sm }} />
            </View>
          ) : error ? (
            <Text style={[typography.small, styles.error]}>
              {extractErrorMessage(error, 'Could not load courses')}
            </Text>
          ) : (
            <View style={styles.empty}>
              <Text style={[typography.body, styles.muted]}>No courses yet.</Text>
              <Text style={[typography.small, styles.muted]}>
                Your school will publish them here.
              </Text>
            </View>
          )
        }
        renderItem={({ item }) => (
          <CourseRow course={item} onPress={() => router.push(`/(app)/courses/${item.id}`)} />
        )}
      />
    </Screen>
  );
}

interface RowProps {
  course: Course;
  onPress: () => void;
}

function CourseRow({ course, onPress }: RowProps) {
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
      <View style={styles.glyph}>
        <Ionicons
          name={iconForSubject(course.subject_type)}
          size={20}
          color={colors.primary}
        />
      </View>
      <View style={{ flex: 1, gap: 2 }}>
        <Text style={typography.h3} numberOfLines={1}>
          {course.title}
        </Text>
        <Text style={[typography.small, styles.muted]} numberOfLines={1}>
          Grade {course.grade_level}
          {course.subject_type ? ` · ${course.subject_type}` : ''}
        </Text>
      </View>
      <Ionicons name="chevron-forward" size={18} color={colors.textSubtle} />
    </Pressable>
  );
}

function iconForSubject(subject: string | null): IoniconName {
  switch ((subject || '').toLowerCase()) {
    case 'math':
      return 'calculator-outline';
    case 'science':
      return 'flask-outline';
    case 'language':
    case 'english':
      return 'book-outline';
    case 'geography':
      return 'earth-outline';
    case 'history':
      return 'time-outline';
    default:
      return 'sparkles-outline';
  }
}

function greetingForHour(): string {
  const h = new Date().getHours();
  if (h < 12) return 'GOOD MORNING';
  if (h < 18) return 'GOOD AFTERNOON';
  return 'GOOD EVENING';
}

const styles = StyleSheet.create({
  list: { paddingBottom: spacing.xxxl },
  headerWrap: {
    paddingHorizontal: spacing.xl,
    paddingTop: spacing.lg,
    gap: spacing.xs,
  },
  muted: { color: colors.textMuted },
  subtitle: { color: colors.textMuted, marginTop: spacing.xs },
  sectionHeader: {
    marginTop: spacing.xxl,
    marginBottom: spacing.sm,
  },
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
  glyph: {
    width: 40,
    height: 40,
    borderRadius: radius.md,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.primarySoft,
  },
  empty: { alignItems: 'center', gap: spacing.xs, paddingTop: spacing.xl },
  error: {
    color: colors.danger,
    paddingHorizontal: spacing.xl,
    marginTop: spacing.lg,
  },
});
