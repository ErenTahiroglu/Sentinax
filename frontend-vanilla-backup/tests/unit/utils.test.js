import { describe, it, expect, vi, beforeEach } from 'vitest';
import { crypto } from 'node:crypto';

// Mock browser globals
const localStorageMock = (() => {
  let store = {};
  return {
    getItem: vi.fn(key => store[key] || null),
    setItem: vi.fn((key, value) => { store[key] = value.toString(); }),
    clear: vi.fn(() => { store = {}; }),
    removeItem: vi.fn(key => { delete store[key]; })
  };
})();

vi.stubGlobal('localStorage', localStorageMock);
vi.stubGlobal('window', global);
vi.stubGlobal('getLang', vi.fn(() => 'tr'));
// In jsdom environment, crypto might be available, but let's ensure it for node if needed
if (!global.crypto) {
    vi.stubGlobal('crypto', crypto);
}

import { fmtNum, colorClass, encryptApiKey, decryptApiKey } from '../../js/utils.js';

describe('Utils.js Logic Tests', () => {
  describe('fmtNum', () => {
    it('should format numbers correctly for tr locale', () => {
      const result = fmtNum(1234.567);
      // In some environments, Intl might use non-breaking spaces or different separators
      // Let's use a more flexible check
      expect(result).toMatch(/1[.,]234[.,]57/);
    });

    it('should return "-" for invalid inputs', () => {
      expect(fmtNum(null)).toBe('-');
      expect(fmtNum(undefined)).toBe('-');
      expect(fmtNum('abc')).toBe('-');
    });

    it('should add suffix if provided', () => {
      const result = fmtNum(10, '%');
      expect(result).toMatch(/10[.,]00%/);
    });
  });

  describe('colorClass', () => {
    it('should return positive for >= 0', () => {
      expect(colorClass(5)).toBe('positive');
      expect(colorClass(0)).toBe('positive');
    });

    it('should return negative for < 0', () => {
      expect(colorClass(-5)).toBe('negative');
    });
  });

  describe('Encryption Logic', () => {
    it('should encrypt and decrypt a string correctly', async () => {
      const original = 'test-api-key';
      const encrypted = await encryptApiKey(original);
      expect(encrypted).toBeDefined();
      expect(encrypted).not.toBe(original);
      
      const decrypted = await decryptApiKey(encrypted);
      expect(decrypted).toBe(original);
    });

    it('should return empty string for empty input', async () => {
      expect(await encryptApiKey('')).toBe('');
      expect(await decryptApiKey('')).toBe('');
    });
  });
});
