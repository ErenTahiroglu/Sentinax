import { vi } from 'vitest';

// Mock missing or DOM-heavy components
vi.mock('../components/CardComponent.js', () => ({
  createSkeletonCard: vi.fn(() => document.createElement('div')),
  createLoadingSpinnerCard: vi.fn(() => document.createElement('div')),
  createMessageCard: vi.fn(() => document.createElement('div')),
  createNewsCard: vi.fn(() => document.createElement('div')),
  createComparisonTable: vi.fn(() => document.createElement('div')),
  createMacroCardHolder: vi.fn(() => document.createElement('div')),
}));

// Provide some globals that might be missing in jsdom or expected by scripts
global.getLang = vi.fn(() => 'tr');
global.t = vi.fn((key) => key);
global.API_BASE = 'http://localhost:8000';
global.AppState = {
    results: [],
    extras: null
};
global.showToast = vi.fn();
global.saveApiKeys = vi.fn();
global.renderMacroAI = vi.fn();
global.renderSingleCard = vi.fn();
global.setCache = vi.fn();
global.getCache = vi.fn();
global.SupabaseAuth = {
    getValidSession: vi.fn().mockResolvedValue({ access_token: 'test-token' })
};
