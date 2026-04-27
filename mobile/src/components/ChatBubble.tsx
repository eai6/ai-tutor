import { StyleSheet, Text, View } from 'react-native';
import Markdown from 'react-native-markdown-display';

import { Avatar } from './Avatar';
import { colors, fonts, radius, spacing, typography } from '@/theme';

export type ChatRole = 'tutor' | 'student' | 'system';

interface Props {
  role: ChatRole;
  content: string;
  showAvatar?: boolean;
  studentInitial?: string;
}

export function ChatBubble({ role, content, showAvatar = true, studentInitial }: Props) {
  const isStudent = role === 'student';
  const isSystem = role === 'system';

  if (isSystem) {
    return (
      <View style={styles.systemRow}>
        <Text style={[typography.caption, styles.systemText]}>{content}</Text>
      </View>
    );
  }

  if (isStudent) {
    return (
      <View style={[styles.row, styles.rowRight]}>
        <View style={styles.studentBubble}>
          <Text style={styles.studentText}>{content}</Text>
        </View>
      </View>
    );
  }

  // Tutor — no bubble. Sparkle avatar floating top-left, markdown body to the right.
  return (
    <View style={styles.tutorRow}>
      <View style={styles.avatarSlot}>
        {showAvatar ? <Avatar variant="tutor" size={28} /> : null}
      </View>
      <View style={styles.tutorBody}>
        <Markdown style={tutorMarkdownStyles}>{content}</Markdown>
      </View>
    </View>
  );
}

const tutorMarkdownStyles = StyleSheet.create({
  body: {
    color: colors.text,
    fontFamily: fonts.serif,
    fontSize: 16,
    lineHeight: 25,
  },
  paragraph: { marginTop: 0, marginBottom: spacing.sm },
  strong: { fontFamily: fonts.serifSemibold },
  em: { fontStyle: 'italic' },
  bullet_list: { marginVertical: spacing.xs },
  ordered_list: { marginVertical: spacing.xs },
  list_item: { marginBottom: spacing.xs },
  heading1: { fontFamily: fonts.uiBold, fontSize: 20, marginVertical: spacing.sm },
  heading2: { fontFamily: fonts.uiSemibold, fontSize: 18, marginVertical: spacing.sm },
  heading3: { fontFamily: fonts.uiSemibold, fontSize: 16, marginVertical: spacing.xs },
  code_inline: {
    backgroundColor: '#0001',
    paddingHorizontal: 4,
    borderRadius: 4,
    fontFamily: 'monospace',
    fontSize: 14,
  },
  code_block: {
    backgroundColor: '#0001',
    padding: spacing.sm,
    borderRadius: radius.sm,
    fontFamily: 'monospace',
    fontSize: 13,
    marginVertical: spacing.xs,
  },
  blockquote: {
    backgroundColor: 'transparent',
    borderLeftWidth: 2,
    borderLeftColor: colors.border,
    paddingHorizontal: spacing.sm,
    paddingVertical: 0,
    marginVertical: spacing.xs,
  },
  link: { color: colors.primary, fontFamily: fonts.serifMedium },
});

const styles = StyleSheet.create({
  row: {
    width: '100%',
    flexDirection: 'row',
    marginBottom: spacing.md,
    paddingHorizontal: spacing.lg,
  },
  rowRight: { justifyContent: 'flex-end' },

  // Tutor: avatar slot + markdown body, no surrounding bubble.
  tutorRow: {
    width: '100%',
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: spacing.md,
    marginBottom: spacing.lg,
    paddingHorizontal: spacing.lg,
  },
  avatarSlot: { width: 28, alignItems: 'center', paddingTop: 2 },
  tutorBody: { flex: 1, paddingTop: 2 },

  studentBubble: {
    maxWidth: '80%',
    paddingVertical: spacing.sm + 2,
    paddingHorizontal: spacing.md + 2,
    borderRadius: radius.xl,
    borderBottomRightRadius: radius.sm,
    backgroundColor: colors.primary,
  },
  studentText: {
    color: colors.primaryText,
    fontFamily: fonts.uiMedium,
    fontSize: 15,
    lineHeight: 21,
  },

  systemRow: { alignItems: 'center', paddingVertical: spacing.xs },
  systemText: { color: colors.textMuted, fontStyle: 'italic' },
});
