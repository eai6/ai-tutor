import { Stack, useLocalSearchParams, useRouter } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { extractErrorMessage } from '@/api/client';
import { getLesson } from '@/api/curriculum';
import { fetchOfflinePack } from '@/api/offline-pack';
import { Screen } from '@/components/Screen';
import { SkeletonBlock } from '@/components/Skeleton';
import { loadPack, savePack } from '@/db/queries/lesson-packs';
import { colors, radius, spacing, typography } from '@/theme';

export default function LessonDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const lessonId = Number(id);
  const router = useRouter();
  const qc = useQueryClient();

  const lessonQ = useQuery({
    queryKey: ['lesson', lessonId],
    queryFn: () => getLesson(lessonId),
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

  if (lessonQ.isLoading) {
    return (
      <Screen scroll={false}>
        <Stack.Screen options={{ title: 'Lesson' }} />
        <View style={styles.contentPad}>
          <SkeletonBlock width={120} height={12} />
          <SkeletonBlock width="80%" height={28} style={{ marginTop: spacing.sm }} />
          <SkeletonBlock width="60%" height={16} style={{ marginTop: spacing.sm }} />
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
  const pack = packQ.data;
  const isDownloaded = !!pack;

  return (
    <Screen scroll={false}>
      <Stack.Screen options={{ title: lesson.unit_title }} />
      <ScrollView contentContainerStyle={styles.content}>
        {/* Hero — large title pattern */}
        <View style={styles.hero}>
          <Text style={[typography.caption, styles.muted]}>
            {lesson.course_title.toUpperCase()}
          </Text>
          <Text style={typography.largeTitle}>{lesson.title}</Text>
          {lesson.objective ? (
            <Text style={[typography.contentBody, styles.objective]}>{lesson.objective}</Text>
          ) : null}
        </View>

        {/* Single purple CTA — chat screen handles cloud vs on-device */}
        <PrimaryCTA
          icon="sparkles"
          label="Start tutor chat"
          onPress={() => router.push(`/(app)/chat/${lessonId}`)}
        />

        {/* Offline pack — status row, not a card */}
        <View style={styles.statusRow}>
          <Ionicons
            name={isDownloaded ? 'checkmark-circle' : 'cloud-download-outline'}
            size={20}
            color={isDownloaded ? colors.success : colors.textMuted}
          />
          <View style={{ flex: 1 }}>
            <Text style={typography.bodyMedium}>
              {isDownloaded ? 'Saved offline' : 'Available offline'}
            </Text>
            <Text style={[typography.small, styles.muted]}>
              {isDownloaded
                ? `Pack v${pack.version} · ${pack.downloaded_at ? new Date(pack.downloaded_at).toLocaleDateString() : ''}`
                : 'Read this lesson + take exit ticket without internet.'}
            </Text>
          </View>
          <View style={{ flexDirection: 'row', gap: spacing.sm }}>
            {isDownloaded ? (
              <>
                <TextButton
                  label="Review"
                  onPress={() => router.push(`/(app)/lessons/${lessonId}/review`)}
                />
                <TextButton
                  label={downloadM.isPending ? '…' : 'Refresh'}
                  loading={downloadM.isPending}
                  onPress={() => downloadM.mutate(true)}
                />
              </>
            ) : (
              <TextButton
                label={downloadM.isPending ? 'Downloading…' : 'Download'}
                loading={downloadM.isPending}
                onPress={() => downloadM.mutate(false)}
              />
            )}
          </View>
        </View>
        {downloadM.error ? (
          <Text style={styles.errorInline}>
            {extractErrorMessage(downloadM.error, 'Download failed')}
          </Text>
        ) : null}
      </ScrollView>
    </Screen>
  );
}

interface CTAProps {
  icon: React.ComponentProps<typeof Ionicons>['name'];
  label: string;
  loading?: boolean;
  onPress: () => void;
}

function PrimaryCTA({ icon, label, loading, onPress }: CTAProps) {
  return (
    <Pressable
      onPress={onPress}
      disabled={loading}
      style={({ pressed }) => [
        styles.cta,
        { opacity: loading ? 0.7 : pressed ? 0.85 : 1 },
      ]}
    >
      {loading ? (
        <ActivityIndicator color={colors.primaryText} />
      ) : (
        <Ionicons name={icon} size={18} color={colors.primaryText} />
      )}
      <Text style={styles.ctaLabel}>{label}</Text>
    </Pressable>
  );
}

interface TextButtonProps {
  label: string;
  loading?: boolean;
  onPress: () => void;
}

function TextButton({ label, loading, onPress }: TextButtonProps) {
  return (
    <Pressable
      onPress={onPress}
      disabled={loading}
      style={({ pressed }) => [styles.textBtn, { opacity: pressed ? 0.6 : 1 }]}
    >
      <Text style={styles.textBtnLabel}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  content: {
    paddingHorizontal: spacing.xl,
    paddingTop: spacing.md,
    paddingBottom: spacing.xxxl,
    gap: spacing.xl,
  },
  contentPad: { padding: spacing.xl, gap: spacing.sm },
  hero: { gap: spacing.xs },
  muted: { color: colors.textMuted },
  objective: { color: colors.text, marginTop: spacing.sm },

  cta: {
    backgroundColor: colors.primary,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.sm,
    paddingVertical: spacing.md + 4,
    paddingHorizontal: spacing.lg,
    borderRadius: radius.pill,
    minHeight: 56,
  },
  ctaLabel: {
    color: colors.primaryText,
    fontFamily: typography.h3.fontFamily,
    fontSize: 16,
  },

  statusRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.md,
    paddingVertical: spacing.md,
    borderTopWidth: 1,
    borderBottomWidth: 1,
    borderColor: colors.borderMuted,
  },
  textBtn: {
    paddingVertical: spacing.xs,
    paddingHorizontal: spacing.sm,
  },
  textBtnLabel: {
    color: colors.text,
    fontFamily: typography.smallMedium.fontFamily,
    fontSize: 14,
  },

  offlineCta: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.sm,
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.lg,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: colors.border,
    backgroundColor: colors.card,
    marginTop: -spacing.sm,
  },
  offlineCtaLabel: {
    color: colors.text,
    fontFamily: typography.smallMedium.fontFamily,
    fontSize: 14,
  },
  errorInline: { color: colors.danger, fontSize: 13, marginTop: -spacing.sm },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  error: { color: colors.danger, fontSize: 14 },
});
