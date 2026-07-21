import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      // Large legacy surface of `any` in widget/data plumbing; keep visible
      // as warnings while errors stay reserved for real defects.
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
  {
    // Electron main process runs in Node, not the browser.
    files: ['electron/**/*.ts'],
    languageOptions: {
      globals: globals.node,
    },
  },
  {
    // shadcn/ui-style files intentionally export variants/hooks alongside
    // components (buttonVariants, useTheme, useDashboard).
    files: [
      'src/components/ui/**/*.tsx',
      'src/components/theme-provider.tsx',
      'src/contexts/**/*.tsx',
    ],
    rules: {
      'react-refresh/only-export-components': 'off',
    },
  },
])
