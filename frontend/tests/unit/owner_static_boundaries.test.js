import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

const root = path.resolve(__dirname, '../..');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');

describe('static boundaries', () => {
  const ownerFiles = ['owner/js/mfa_flow.js', 'owner/js/owner_app.js', 'owner/index.html'];

  it('owner surface has no signup/OAuth/OTP entry points and no unsafe sinks or storage', () => {
    for (const f of ownerFiles) {
      const src = read(f);
      for (const banned of ['signUp', 'signInWithOAuth', 'signInWithOtp', 'signInAnonymously', 'innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'localStorage', 'sessionStorage', 'indexedDB', 'console.log', 'admin_bypass', 'mock_bypass_token']) {
        expect(src, `${f} must not contain ${banned}`).not.toContain(banned);
      }
    }
  });

  it('owner page has a strict CSP, no inline script/handlers and a pinned library', () => {
    const html = read('owner/index.html');
    const csp = html.match(/Content-Security-Policy"\s+content="([^"]+)"/)[1];
    expect(csp).not.toMatch(/unsafe-inline|unsafe-eval/);
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("base-uri 'none'");
    expect(csp).toContain("form-action 'none'");
    expect(html).not.toMatch(/<script(?![^>]*\bsrc=)/i);
    expect(html).not.toMatch(/\son[a-z]+\s*=/i);
    expect(html).toContain('name="referrer" content="no-referrer"');
    const app = read('owner/js/owner_app.js');
    expect(app).toMatch(/@supabase\/supabase-js@\d+\.\d+\.\d+\//);
    expect(app).not.toMatch(/supabase-js(@latest|\/\+esm)/);
  });

  it('legacy client no longer fabricates a user or token and gates on aal2', () => {
    const c = read('js/network/supabaseClient.js');
    expect(c).not.toContain('mock_bypass_token');
    expect(c).not.toContain('dev_admin');
    expect(c).not.toMatch(/getItem\(["']admin_bypass["']\)/);
    expect(c).toContain('isAal2Session');
    const admin = read('js/components/AdminDashboard.js');
    expect(admin).not.toContain('admin_bypass');
  });

  it('legacy client never calls the provider for sign-up or OAuth (public sign-up is closed)', () => {
    const c = read('js/network/supabaseClient.js');
    expect(c).not.toMatch(/auth\.signUp\(/);
    expect(c).not.toMatch(/signInWithOAuth\(/);
    expect(c).not.toMatch(/signInWithOtp\(/);
  });

  it('public Buffett frontend is isolated from auth, owner and Supabase code', () => {
    const dir = path.join(root, 'buffett');
    const files = [];
    const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).forEach((e) => (e.isDirectory() ? walk(path.join(d, e.name)) : files.push(path.join(d, e.name))));
    walk(dir);
    for (const f of files) {
      const src = fs.readFileSync(f, 'utf8').toLowerCase();
      for (const t of ['supabase', 'owner/', 'aalgate', 'authorization', 'access_token', 'mfa']) {
        expect(src, `${f} must not reference ${t}`).not.toContain(t);
      }
    }
  });
});
