import { ReactNode } from 'react';
import { Pressable, StyleSheet, View, ViewStyle } from 'react-native';

import { colors, elevation, radius, spacing } from '@/theme';

interface Props {
  children: ReactNode;
  onPress?: () => void;
  style?: ViewStyle;
  flat?: boolean;
}

export function Card({ children, onPress, style, flat }: Props) {
  const base = [styles.card, flat ? styles.flat : elevation, style];
  if (onPress) {
    return (
      <Pressable
        onPress={onPress}
        style={(state) => {
          // react-native-web exposes `hovered` on the state object.
          const hovered = (state as { hovered?: boolean }).hovered;
          return [
            ...base,
            hovered ? styles.hovered : null,
            state.pressed ? styles.pressed : null,
          ];
        }}
      >
        {children}
      </Pressable>
    );
  }
  return <View style={base}>{children}</View>;
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.card,
    borderRadius: radius.lg,
    padding: spacing.lg,
    borderWidth: 1,
    borderColor: colors.borderMuted,
  },
  flat: {
    backgroundColor: colors.bgMuted,
    borderColor: colors.borderMuted,
  },
  hovered: {
    borderColor: colors.border,
    transform: [{ translateY: -1 }],
  },
  pressed: {
    opacity: 0.92,
    transform: [{ translateY: 0 }],
  },
});
