-- LlamaNet Gateway — Supabase Schema
-- Run in Supabase SQL Editor to initialize all required tables

-- 1. Users table (Google OAuth + manual registration)
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    full_name TEXT DEFAULT '',
    avatar_url TEXT DEFAULT '',
    google_id TEXT DEFAULT '',
    last_login TIMESTAMPTZ DEFAULT now(),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 2. API keys table
CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    key_hash TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    name TEXT DEFAULT 'default',
    is_active BOOLEAN DEFAULT true,
    last_used TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 3. Nodes table (inference node registry)
CREATE TABLE IF NOT EXISTS nodes (
    node_hash VARCHAR(12) PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    model_name TEXT NOT NULL,
    model_slug TEXT NOT NULL,
    url TEXT DEFAULT '',
    ip TEXT DEFAULT '',
    port INTEGER DEFAULT 8000,
    gpu_info TEXT DEFAULT '',
    load FLOAT DEFAULT 0,
    tps FLOAT DEFAULT 0,
    ttft FLOAT,
    latency FLOAT,
    uptime INTEGER DEFAULT 0,
    total_tokens BIGINT DEFAULT 0,
    status TEXT DEFAULT 'active',
    last_heartbeat TIMESTAMPTZ DEFAULT now(),
    pool_models JSONB DEFAULT '[]'::jsonb,
    metrics JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 4. Global statistics (cumulative token tracking across node restarts)
CREATE TABLE IF NOT EXISTS global_statistics (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- 5. Tunnel state (Cloudflare tunnel persistence for named tunnels)
CREATE TABLE IF NOT EXISTS tunnel_state (
    node_hash VARCHAR(12) PRIMARY KEY,
    tunnel_id TEXT NOT NULL,
    tunnel_token TEXT NOT NULL,
    hostname TEXT NOT NULL,
    dns_record_id TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_nodes_model_slug ON nodes(model_slug);
CREATE INDEX IF NOT EXISTS idx_nodes_user_id ON nodes(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_key_hash ON api_keys(key_hash);

-- Seed system user for public node registration (no auth required)
-- Note: The gateway server auto-creates this user at startup if missing
-- INSERT INTO users (id, email, full_name) VALUES
--   ('00000000-0000-0000-0000-000000000000', 'system@llamanet.app', 'LlamaNet System');

-- 6. Add per-node bearer token column
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS node_token TEXT;

-- 7. Token usage tracking (per-API-key daily budgets)
CREATE TABLE IF NOT EXISTS token_usage (
    key_hash TEXT NOT NULL,
    usage_date DATE NOT NULL,
    tokens_consumed INTEGER NOT NULL DEFAULT 0,
    requests_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_hash, usage_date)
);

-- Set default via ALTER to avoid inline DEFAULT in CREATE TABLE
ALTER TABLE token_usage ALTER COLUMN usage_date SET DEFAULT CURRENT_DATE;

CREATE INDEX IF NOT EXISTS idx_token_usage_key_date ON token_usage(key_hash, usage_date);

-- 8. Context length per model on each node (junction table)
CREATE TABLE IF NOT EXISTS node_models (
    node_hash   TEXT NOT NULL,
    model_name  TEXT NOT NULL,
    model_slug  TEXT NOT NULL,
    ctx_length  INTEGER DEFAULT 0,
    is_active   BOOLEAN DEFAULT false,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (node_hash, model_slug)
);

CREATE INDEX IF NOT EXISTS idx_node_models_slug ON node_models(model_slug);
CREATE INDEX IF NOT EXISTS idx_node_models_ctx ON node_models(ctx_length);

-- 9. Add context length to nodes table for primary model
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS ctx_length INTEGER DEFAULT 0;

-- 10. Remove redundant model columns from nodes table
-- node_models junction table is now the single source of truth for model info
DROP INDEX IF EXISTS idx_nodes_model_slug;
ALTER TABLE nodes DROP COLUMN IF EXISTS model_name;
ALTER TABLE nodes DROP COLUMN IF EXISTS model_slug;
