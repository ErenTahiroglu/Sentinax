import { defineConfig } from 'vitest/config';
import path from 'path';

export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./frontend-vanilla-backup/tests/setup.js'],
    exclude: [
      '**/node_modules/**',
      '**/dist/**',
      '**/cypress/**',
      '**/.{idea,git,cache,output,temp}/**',
      'frontend-vanilla-backup/**',
      'tests/e2e/**'
    ],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
      include: ['frontend/src/**/*.ts', 'frontend/src/**/*.tsx'],
      exclude: [
        'frontend/src/vendor/**', 
        'frontend/tests/**', 
        'frontend/src/app/layout.tsx', 
        'frontend/src/app/page.tsx', 
        'frontend/src/utils/supabase/**'
      ],
    },
  },
  resolve: {
    alias: {
      '/frontend': path.resolve(__dirname, './frontend'),
      '@': path.resolve(__dirname, './frontend/src'),
      'react': path.resolve(__dirname, './frontend/node_modules/react'),
      'react-dom': path.resolve(__dirname, './frontend/node_modules/react-dom'),
      '@testing-library/react': path.resolve(__dirname, './frontend/node_modules/@testing-library/react'),
      '@testing-library/jest-dom': path.resolve(__dirname, './frontend/node_modules/@testing-library/jest-dom'),
    },
  },
});
