import { Platform } from 'react-native';

export const colors = {
  bg: '#fafaf9',
  bgMuted: '#f4f4f2',
  surface: '#f0eeec',
  card: '#ffffff',
  border: '#e7e5e4',
  borderMuted: '#f0eeec',
  text: '#1c1917',
  textMuted: '#78716c',
  textSubtle: '#a8a29e',
  primary: '#7c3aed',
  primarySoft: '#ede9fe',
  primaryText: '#ffffff',
  accent: '#f97316',
  danger: '#dc2626',
  dangerSoft: '#fee2e2',
  success: '#16a34a',
  successSoft: '#dcfce7',
  warning: '#d97706',
  warningSoft: '#fef3c7',
};

export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
  xxxl: 48,
};

export const radius = {
  sm: 6,
  md: 10,
  lg: 14,
  xl: 20,
  pill: 9999,
};

export const fonts = {
  ui: 'Inter_400Regular',
  uiMedium: 'Inter_500Medium',
  uiSemibold: 'Inter_600SemiBold',
  uiBold: 'Inter_700Bold',
  uiExtraBold: 'Inter_800ExtraBold',
  serif: 'Lora_400Regular',
  serifMedium: 'Lora_500Medium',
  serifSemibold: 'Lora_600SemiBold',
};

export const typography = {
  // Apple-Books-style large title for screen openers.
  largeTitle: {
    fontFamily: fonts.uiBold,
    fontSize: 32,
    lineHeight: 38,
    letterSpacing: -0.6,
  },
  h1: {
    fontFamily: fonts.uiBold,
    fontSize: 26,
    lineHeight: 32,
    letterSpacing: -0.4,
  },
  h2: {
    fontFamily: fonts.uiSemibold,
    fontSize: 20,
    lineHeight: 26,
    letterSpacing: -0.2,
  },
  h3: {
    fontFamily: fonts.uiSemibold,
    fontSize: 17,
    lineHeight: 22,
  },
  body: {
    fontFamily: fonts.ui,
    fontSize: 16,
    lineHeight: 22,
  },
  bodyMedium: {
    fontFamily: fonts.uiMedium,
    fontSize: 16,
    lineHeight: 22,
  },
  small: {
    fontFamily: fonts.ui,
    fontSize: 14,
    lineHeight: 20,
  },
  smallMedium: {
    fontFamily: fonts.uiMedium,
    fontSize: 14,
    lineHeight: 20,
  },
  caption: {
    fontFamily: fonts.uiSemibold,
    fontSize: 11,
    lineHeight: 14,
    letterSpacing: 0.6,
  },
  // Lora — used for tutor responses + lesson body content.
  contentBody: {
    fontFamily: fonts.serif,
    fontSize: 16,
    lineHeight: 25,
  },
  contentBodyMedium: {
    fontFamily: fonts.serifMedium,
    fontSize: 16,
    lineHeight: 25,
  },
};

export const elevation = (Platform.OS === 'web'
  ? ({ boxShadow: '0 1px 2px rgba(28, 25, 23, 0.04)' } as unknown as object)
  : {
      shadowColor: '#1c1917',
      shadowOffset: { width: 0, height: 1 },
      shadowOpacity: 0.04,
      shadowRadius: 2,
      elevation: 1,
    }) as object;
