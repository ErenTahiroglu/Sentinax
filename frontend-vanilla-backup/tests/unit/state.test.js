import { describe, it, expect, vi } from 'vitest';
import { createStore } from '../../js/core/state.js';

describe('State Manager v2 Tests', () => {
  it('should initialize with provided state', () => {
    const initial = { count: 0, user: 'test' };
    const store = createStore(initial);
    expect(store.count).toBe(0);
    expect(store.user).toBe('test');
  });

  it('should support granular property-based subscriptions', () => {
    const store = createStore({ a: 1, b: 2 });
    const listenerA = vi.fn();
    const listenerB = vi.fn();

    store.subscribe('a', listenerA);
    store.subscribe('b', listenerB);

    // Initial calls
    expect(listenerA).toHaveBeenCalledWith(1, undefined, expect.anything());
    expect(listenerB).toHaveBeenCalledWith(2, undefined, expect.anything());

    listenerA.mockClear();
    listenerB.mockClear();

    store.a = 10;
    expect(listenerA).toHaveBeenCalledWith(10, 1, expect.anything());
    expect(listenerB).not.toHaveBeenCalled();
  });

  it('should support global subscriptions (backward compatibility)', () => {
    const store = createStore({ count: 0 });
    const listener = vi.fn();
    store.subscribe(listener);
    
    expect(listener).toHaveBeenCalledWith('count', 0, undefined, expect.anything());
    
    listener.mockClear();
    store.count = 1;
    expect(listener).toHaveBeenCalledWith('count', 1, 0, expect.anything());
  });

  it('should not notify if value is identical', () => {
    const store = createStore({ count: 0 });
    const listener = vi.fn();
    store.subscribe('count', listener);
    listener.mockClear();
    
    store.count = 0;
    expect(listener).not.toHaveBeenCalled();
  });

  it('should allow granular unsubscription', () => {
    const store = createStore({ a: 1 });
    const listener = vi.fn();
    const unsubscribe = store.subscribe('a', listener);
    
    listener.mockClear();
    unsubscribe();
    
    store.a = 2;
    expect(listener).not.toHaveBeenCalled();
  });
});
