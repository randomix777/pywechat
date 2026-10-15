-- Run against JYI's jyi-ai-platform D1 database.
-- Public desktop client: no client secret. PKCE S256 is required by JYI.
-- JYI allows any port on the same loopback host and callback path.
INSERT OR IGNORE INTO ai_oauth_clients
    (id, name, redirect_uris_json, is_public, created_at)
VALUES (
    'pyweixin-desktop',
    '微信AI助理',
    '["http://127.0.0.1:18080/oauth/callback"]',
    1,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);
