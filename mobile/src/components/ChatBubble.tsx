import { StyleSheet, Text, View } from 'react-native';

import { colors, radius, spacing, typography } from '@/theme';

export type ChatRole = 'tutor' | 'student' | 'system';

interface Props {
  role: ChatRole;
  content: string;
  pending?: boolean;
}

export function ChatBubble({ role, content, pending }: Props) {
  const isStudent = role === 'student';
  const isSystem = role === 'system';

  if (isSystem) {
    return (
      <View style={styles.systemRow}>
        <Text style={[typography.caption, styles.systemText]}>{content}</Text>
      </View>
    );
  }

  return (
    <View style={[styles.row, isStudent ? styles.rowRight : styles.rowLeft]}>
      <View
        style={[
          styles.bubble,
          isStudent ? styles.studentBubble : styles.tutorBubble,
          pending ? styles.pending : null,
        ]}
      >
        <Text style={[styles.text, isStudent ? styles.studentText : styles.tutorText]}>
          {content}
        </Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: { width: '100%', marginBottom: spacing.sm, paddingHorizontal: spacing.lg },
  rowLeft: { alignItems: 'flex-start' },
  rowRight: { alignItems: 'flex-end' },
  bubble: {
    maxWidth: '85%',
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.md,
    borderRadius: radius.lg,
  },
  tutorBubble: {
    backgroundColor: colors.bgMuted,
    borderTopLeftRadius: radius.sm,
  },
  studentBubble: {
    backgroundColor: colors.primary,
    borderTopRightRadius: radius.sm,
  },
  pending: { opacity: 0.6 },
  text: { fontSize: 15, lineHeight: 21 },
  tutorText: { color: colors.text },
  studentText: { color: colors.primaryText },
  systemRow: { alignItems: 'center', paddingVertical: spacing.xs },
  systemText: { color: colors.textMuted, fontStyle: 'italic' },
});
