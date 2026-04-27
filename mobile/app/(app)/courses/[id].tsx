import { useEffect, useMemo, useState } from 'react';
import { Stack, useLocalSearchParams, useRouter } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import {
  LayoutAnimation,
  Platform,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  UIManager,
  View,
} from 'react-native';
import { useQuery } from '@tanstack/react-query';

import { listLessons, listProgress } from '@/api/curriculum';
import { extractErrorMessage } from '@/api/client';
import { Screen } from '@/components/Screen';
import { SkeletonBlock } from '@/components/Skeleton';
import { colors, fonts, radius, spacing, typography } from '@/theme';
import type { Lesson, ProgressRow } from '@/api/types';

// Enable smooth expand/collapse on Android.
if (Platform.OS === 'android' && UIManager.setLayoutAnimationEnabledExperimental) {
  UIManager.setLayoutAnimationEnabledExperimental(true);
}

interface UnitGroup {
  unit_id: number;
  unit_title: string;
  lessons: Lesson[];
}

function groupLessonsByUnit(lessons: Lesson[]): UnitGroup[] {
  const map = new Map<number, UnitGroup>();
  for (const lesson of lessons) {
    let group = map.get(lesson.unit_id);
    if (!group) {
      group = { unit_id: lesson.unit_id, unit_title: lesson.unit_title, lessons: [] };
      map.set(lesson.unit_id, group);
    }
    group.lessons.push(lesson);
  }
  return Array.from(map.values());
}

/**
 * The unit holding the lesson with the most recent activity. Falls
 * back to the first unit when the student hasn't touched anything yet.
 */
function pickInitialUnit(groups: UnitGroup[], progress: ProgressRow[]): number | null {
  if (groups.length === 0) return null;
  const lessonIds = new Set<number>(
    groups.flatMap((g) => g.lessons.map((l) => l.id)),
  );
  const candidates = progress
    .filter((p) => lessonIds.has(p.lesson_id) && p.last_session_at)
    .sort(
      (a, b) =>
        new Date(b.last_session_at ?? 0).getTime() -
        new Date(a.last_session_at ?? 0).getTime(),
    );
  if (candidates.length > 0) {
    const target = candidates[0].lesson_id;
    const found = groups.find((g) => g.lessons.some((l) => l.id === target));
    if (found) return found.unit_id;
  }
  return groups[0].unit_id;
}

export default function CourseLessonsScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const courseId = Number(id);
  const router = useRouter();

  const lessonsQ = useQuery({
    queryKey: ['lessons', { courseId }],
    queryFn: () => listLessons(courseId),
    enabled: Number.isFinite(courseId),
  });
  const progressQ = useQuery({
    queryKey: ['progress'],
    queryFn: listProgress,
    // Progress feeds the "most recent unit" heuristic; if it 404s we
    // just open the first unit instead.
  });

  const groups = useMemo(
    () => groupLessonsByUnit(lessonsQ.data ?? []),
    [lessonsQ.data],
  );
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [didInit, setDidInit] = useState(false);

  // On first data arrival, expand the unit holding the most-recent
  // lesson (or the first unit). Won't re-fire if the user collapses it.
  useEffect(() => {
    if (didInit) return;
    if (groups.length === 0) return;
    const initial = pickInitialUnit(groups, progressQ.data ?? []);
    if (initial != null) {
      setExpanded(new Set([initial]));
      setDidInit(true);
    }
  }, [groups, progressQ.data, didInit]);

  function toggleUnit(unitId: number) {
    LayoutAnimation.configureNext(LayoutAnimation.Presets.easeInEaseOut);
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(unitId)) next.delete(unitId);
      else next.add(unitId);
      return next;
    });
  }

  if (lessonsQ.isLoading) {
    return (
      <Screen scroll={false}>
        <Stack.Screen options={{ title: 'Lessons' }} />
        <View style={{ paddingHorizontal: spacing.xl, paddingTop: spacing.lg }}>
          <SkeletonBlock height={20} width="70%" />
          <SkeletonBlock height={14} width="40%" style={{ marginTop: spacing.sm }} />
          <SkeletonBlock height={20} width="60%" style={{ marginTop: spacing.xl }} />
          <SkeletonBlock height={14} width="50%" style={{ marginTop: spacing.sm }} />
        </View>
      </Screen>
    );
  }

  if (lessonsQ.error) {
    return (
      <Screen scroll={false}>
        <Stack.Screen options={{ title: 'Lessons' }} />
        <Text style={[typography.small, styles.error]}>
          {extractErrorMessage(lessonsQ.error, 'Could not load lessons')}
        </Text>
      </Screen>
    );
  }

  if (groups.length === 0) {
    return (
      <Screen scroll={false}>
        <Stack.Screen options={{ title: 'Lessons' }} />
        <View style={styles.empty}>
          <Text style={[typography.body, styles.muted]}>No lessons yet.</Text>
        </View>
      </Screen>
    );
  }

  return (
    <Screen scroll={false}>
      <Stack.Screen options={{ title: 'Lessons' }} />
      <ScrollView
        contentContainerStyle={styles.scroll}
        refreshControl={
          <RefreshControl
            refreshing={lessonsQ.isRefetching || progressQ.isRefetching}
            onRefresh={() => {
              void lessonsQ.refetch();
              void progressQ.refetch();
            }}
            tintColor={colors.text}
          />
        }
      >
        {groups.map((group) => {
          const isOpen = expanded.has(group.unit_id);
          return (
            <View key={group.unit_id} style={styles.unitWrap}>
              <Pressable
                onPress={() => toggleUnit(group.unit_id)}
                style={(state) => {
                  const hovered = (state as { hovered?: boolean }).hovered;
                  return [
                    styles.unitHeader,
                    hovered && styles.unitHeaderHover,
                    state.pressed && { opacity: 0.7 },
                  ];
                }}
              >
                <View style={{ flex: 1 }}>
                  <Text style={[typography.caption, styles.unitEyebrow]}>
                    UNIT · {group.lessons.length}{' '}
                    {group.lessons.length === 1 ? 'lesson' : 'lessons'}
                  </Text>
                  <Text style={typography.h3} numberOfLines={2}>
                    {group.unit_title}
                  </Text>
                </View>
                <Ionicons
                  name={isOpen ? 'chevron-down' : 'chevron-forward'}
                  size={20}
                  color={colors.textSubtle}
                />
              </Pressable>

              {isOpen ? (
                <View style={styles.lessonList}>
                  {group.lessons.map((lesson, idx) => (
                    <Pressable
                      key={lesson.id}
                      onPress={() => router.push(`/(app)/lessons/${lesson.id}`)}
                      style={(state) => {
                        const hovered = (state as { hovered?: boolean }).hovered;
                        return [
                          styles.row,
                          hovered && styles.rowHover,
                          state.pressed && styles.rowPressed,
                        ];
                      }}
                    >
                      <View style={styles.indexCircle}>
                        <Text style={styles.indexText}>{idx + 1}</Text>
                      </View>
                      <View style={{ flex: 1, gap: 2 }}>
                        <Text style={typography.h3} numberOfLines={2}>
                          {lesson.title}
                        </Text>
                        {lesson.objective ? (
                          <Text style={[typography.small, styles.muted]} numberOfLines={2}>
                            {lesson.objective}
                          </Text>
                        ) : null}
                      </View>
                      <Ionicons name="chevron-forward" size={18} color={colors.textSubtle} />
                    </Pressable>
                  ))}
                </View>
              ) : null}
            </View>
          );
        })}
      </ScrollView>
    </Screen>
  );
}

const styles = StyleSheet.create({
  scroll: { paddingTop: spacing.md, paddingBottom: spacing.xxxl },
  unitWrap: {
    marginBottom: spacing.md,
  },
  unitHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: spacing.xl,
    paddingVertical: spacing.md + 4,
    backgroundColor: colors.bg,
  },
  unitHeaderHover: { backgroundColor: colors.bgMuted },
  unitEyebrow: {
    color: colors.textMuted,
    letterSpacing: 0.6,
    marginBottom: 2,
  },
  lessonList: {
    backgroundColor: colors.bgMuted,
    borderRadius: radius.md,
    marginHorizontal: spacing.lg,
    paddingVertical: spacing.xs,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.md,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  },
  rowHover: { backgroundColor: colors.borderMuted },
  rowPressed: { opacity: 0.6 },
  indexCircle: {
    width: 32,
    height: 32,
    borderRadius: 16,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: colors.primarySoft,
  },
  indexText: {
    color: colors.primary,
    fontFamily: fonts.uiSemibold,
    fontSize: 14,
  },
  muted: { color: colors.textMuted },
  empty: { alignItems: 'center', paddingTop: spacing.xl },
  error: {
    color: colors.danger,
    paddingHorizontal: spacing.xl,
    marginTop: spacing.lg,
  },
});
