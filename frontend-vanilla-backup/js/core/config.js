/**
 * ⚙️ Global Configuration (SRE Production Stack)
 * ============================================
 * Centralized API and Service configurations.
 */

const API_BASE = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1" 
    ? "http://localhost:8000" 
    : "https://ai-portfoy-yoneticisi.onrender.com";

// 🛡️ Supabase Configuration
const SUPABASE_URL = "https://zlggrmsolklhfgijcjnz.supabase.co";
const SUPABASE_ANON_KEY = "sb_publishable_G67qSQ6JxmUYy3fuQAHb_Q_myhSebvW";

// 🛡️ Expose to Global Scope
window.API_BASE = API_BASE;
window.SUPABASE_URL = SUPABASE_URL;
window.SUPABASE_ANON_KEY = SUPABASE_ANON_KEY;
