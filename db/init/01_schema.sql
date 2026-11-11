-- =============================================================================
-- Hextgen HMS - Database Schema Initialization
-- File: db/init/01_schema.sql
-- =============================================================================

-- Ensure required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Create application role for FastAPI API (used with RLS) if it does not exist
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hms_app') THEN
        CREATE ROLE hms_app WITH LOGIN PASSWORD 'hms_app_password';
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 1. Hospitals (Tenants)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hospitals (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    city VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------------------------------------
-- 2. Users
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------------------------------------
-- 3. Roles
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT
);

-- -----------------------------------------------------------------------------
-- 4. Permissions
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS permissions (
    id SERIAL PRIMARY KEY,
    code VARCHAR(100) UNIQUE NOT NULL,
    description TEXT
);

-- -----------------------------------------------------------------------------
-- 5. Role Permissions (with JSONB constraints)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS role_permissions (
    id SERIAL PRIMARY KEY,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    constraints JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_role_permission UNIQUE (role_id, permission_id)
);

-- -----------------------------------------------------------------------------
-- 6. User Roles
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_roles (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    CONSTRAINT uq_user_role UNIQUE (user_id, role_id)
);

-- -----------------------------------------------------------------------------
-- 7. User Permission Overrides (grant or revoke specific permissions per user)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_permission_overrides (
    id SERIAL PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    effect VARCHAR(20) NOT NULL CHECK (effect IN ('grant', 'revoke', 'deny')),
    constraints JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_user_permission UNIQUE (user_id, permission_id)
);

-- -----------------------------------------------------------------------------
-- 8. Inventory Items
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory_items (
    id SERIAL PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(100) NOT NULL,
    batch_no VARCHAR(100) NOT NULL,
    expiry_date DATE NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    unit_price NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------------------------------------
-- 9. Bills
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bills (
    id SERIAL PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    patient_name VARCHAR(255) NOT NULL,
    bill_date DATE NOT NULL,
    gross_amount NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
    discount_pct NUMERIC(5, 2) NOT NULL DEFAULT 0.00,
    net_amount NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
    status VARCHAR(50) NOT NULL DEFAULT 'unpaid',
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------------------------------------
-- 10. Pending Actions (Two-phase write preview and confirmation tokens)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_actions (
    id SERIAL PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    intent JSONB NOT NULL,
    preview JSONB NOT NULL,
    token VARCHAR(255) UNIQUE NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------------------------------------
-- 11. Audit Log (every attempt, refusal, clarify, preview, cancel, error)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    raw_query TEXT NOT NULL,
    parsed_intent JSONB,
    decision VARCHAR(50) NOT NULL,
    reason TEXT,
    status VARCHAR(50) NOT NULL DEFAULT 'logged',
    rows_affected INTEGER NOT NULL DEFAULT 0,
    before_snapshot JSONB,
    after_snapshot JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------------------------------------
-- Indices for Performance and Multi-Tenant Lookups
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_users_hospital ON users(hospital_id);
CREATE INDEX IF NOT EXISTS idx_inventory_hospital ON inventory_items(hospital_id);
CREATE INDEX IF NOT EXISTS idx_inventory_expiry ON inventory_items(hospital_id, expiry_date);
CREATE INDEX IF NOT EXISTS idx_inventory_quantity ON inventory_items(hospital_id, quantity);
CREATE INDEX IF NOT EXISTS idx_bills_hospital_date ON bills(hospital_id, bill_date);
CREATE INDEX IF NOT EXISTS idx_bills_created_by ON bills(hospital_id, created_by_user_id);
CREATE INDEX IF NOT EXISTS idx_pending_actions_token ON pending_actions(token);
CREATE INDEX IF NOT EXISTS idx_audit_log_hospital ON audit_log(hospital_id, created_at);

-- -----------------------------------------------------------------------------
-- Seed System Roles
-- -----------------------------------------------------------------------------
INSERT INTO roles (name, description) VALUES
    ('Admin', 'Full administrative access across all hospital operations'),
    ('Billing manager', 'Billing department lead with discount and status management permissions'),
    ('Pharmacist', 'Pharmacy staff managing medication inventory'),
    ('Front desk', 'Reception and billing staff managing patient check-in and own invoices'),
    ('Doctor', 'Clinical medical staff with read access to medications and patients')
ON CONFLICT (name) DO NOTHING;

-- -----------------------------------------------------------------------------
-- Seed System Permissions (12 permissions)
-- -----------------------------------------------------------------------------
INSERT INTO permissions (code, description) VALUES
    ('inventory.read', 'Read inventory items, stock levels, and expiry dates'),
    ('inventory.update', 'Update stock quantity and details for inventory items'),
    ('inventory.delete', 'Delete inventory items'),
    ('inventory.create', 'Add new inventory items'),
    ('bills.read', 'Read invoices and billing records'),
    ('bills.discount', 'Apply percentage discount to bills'),
    ('bills.update_status', 'Update billing status (e.g. paid, unpaid, cancelled)'),
    ('bills.create', 'Create new bills for patients'),
    ('users.read', 'View user accounts, contact details, and role assignments'),
    ('users.manage_access', 'Grant or revoke roles and permissions for users'),
    ('reports.read', 'View operational and clinical reports'),
    ('patients.read', 'View patient demographics and records')
ON CONFLICT (code) DO NOTHING;

-- -----------------------------------------------------------------------------
-- Seed Role Permissions Matrix (with JSONB constraints)
-- -----------------------------------------------------------------------------
-- Admin: all permissions (bills.discount max_pct: 100, others {})
INSERT INTO role_permissions (role_id, permission_id, constraints)
SELECT r.id, p.id,
    CASE
        WHEN p.code = 'bills.discount' THEN '{"max_pct": 100}'::jsonb
        ELSE '{}'::jsonb
    END
FROM roles r, permissions p
WHERE r.name = 'Admin'
ON CONFLICT (role_id, permission_id) DO UPDATE SET constraints = EXCLUDED.constraints;

-- Billing manager: bills.read, bills.discount (max_pct: 10), bills.update_status, bills.create
INSERT INTO role_permissions (role_id, permission_id, constraints)
SELECT r.id, p.id,
    CASE
        WHEN p.code = 'bills.discount' THEN '{"max_pct": 10}'::jsonb
        ELSE '{}'::jsonb
    END
FROM roles r, permissions p
WHERE r.name = 'Billing manager'
  AND p.code IN ('bills.read', 'bills.discount', 'bills.update_status', 'bills.create')
ON CONFLICT (role_id, permission_id) DO UPDATE SET constraints = EXCLUDED.constraints;

-- Pharmacist: inventory.read, inventory.update (max_rows: 500), inventory.create
INSERT INTO role_permissions (role_id, permission_id, constraints)
SELECT r.id, p.id,
    CASE
        WHEN p.code = 'inventory.update' THEN '{"max_rows": 500}'::jsonb
        ELSE '{}'::jsonb
    END
FROM roles r, permissions p
WHERE r.name = 'Pharmacist'
  AND p.code IN ('inventory.read', 'inventory.update', 'inventory.create')
ON CONFLICT (role_id, permission_id) DO UPDATE SET constraints = EXCLUDED.constraints;

-- Front desk: bills.read (own_only: true), bills.create
INSERT INTO role_permissions (role_id, permission_id, constraints)
SELECT r.id, p.id,
    CASE
        WHEN p.code = 'bills.read' THEN '{"own_only": true}'::jsonb
        ELSE '{}'::jsonb
    END
FROM roles r, permissions p
WHERE r.name = 'Front desk'
  AND p.code IN ('bills.read', 'bills.create')
ON CONFLICT (role_id, permission_id) DO UPDATE SET constraints = EXCLUDED.constraints;

-- Doctor: inventory.read, patients.read
INSERT INTO role_permissions (role_id, permission_id, constraints)
SELECT r.id, p.id, '{}'::jsonb
FROM roles r, permissions p
WHERE r.name = 'Doctor'
  AND p.code IN ('inventory.read', 'patients.read')
ON CONFLICT (role_id, permission_id) DO UPDATE SET constraints = EXCLUDED.constraints;

-- -----------------------------------------------------------------------------
-- Row-Level Security (RLS) Configuration
-- -----------------------------------------------------------------------------
ALTER TABLE inventory_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE bills ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_permission_overrides ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS inventory_tenant_isolation ON inventory_items;
CREATE POLICY inventory_tenant_isolation ON inventory_items
    FOR ALL
    USING (hospital_id = NULLIF(current_setting('app.hospital_id', true), '')::INTEGER)
    WITH CHECK (hospital_id = NULLIF(current_setting('app.hospital_id', true), '')::INTEGER);

DROP POLICY IF EXISTS bills_tenant_isolation ON bills;
CREATE POLICY bills_tenant_isolation ON bills
    FOR ALL
    USING (hospital_id = NULLIF(current_setting('app.hospital_id', true), '')::INTEGER)
    WITH CHECK (hospital_id = NULLIF(current_setting('app.hospital_id', true), '')::INTEGER);

DROP POLICY IF EXISTS pending_actions_tenant_isolation ON pending_actions;
CREATE POLICY pending_actions_tenant_isolation ON pending_actions
    FOR ALL
    USING (hospital_id = NULLIF(current_setting('app.hospital_id', true), '')::INTEGER)
    WITH CHECK (hospital_id = NULLIF(current_setting('app.hospital_id', true), '')::INTEGER);

DROP POLICY IF EXISTS audit_log_tenant_isolation ON audit_log;
CREATE POLICY audit_log_tenant_isolation ON audit_log
    FOR ALL
    USING (hospital_id = NULLIF(current_setting('app.hospital_id', true), '')::INTEGER)
    WITH CHECK (hospital_id = NULLIF(current_setting('app.hospital_id', true), '')::INTEGER);

DROP POLICY IF EXISTS overrides_tenant_isolation ON user_permission_overrides;
CREATE POLICY overrides_tenant_isolation ON user_permission_overrides
    FOR ALL
    USING (hospital_id = NULLIF(current_setting('app.hospital_id', true), '')::INTEGER)
    WITH CHECK (hospital_id = NULLIF(current_setting('app.hospital_id', true), '')::INTEGER);

-- -----------------------------------------------------------------------------
-- Grants for hms_app
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO hms_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hms_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hms_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hms_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO hms_app;

-- -----------------------------------------------------------------------------
-- Auth Lookup Function
-- Returns user details, assigned roles, and effective permissions
-- Effective permissions = role permissions + 'grant' overrides - 'deny'/'revoke' overrides
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION auth_lookup(p_identifier VARCHAR)
RETURNS TABLE (
    user_id INTEGER,
    hospital_id INTEGER,
    name VARCHAR,
    email VARCHAR,
    is_active BOOLEAN,
    password_hash VARCHAR,
    roles JSONB,
    effective_permissions JSONB
) LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    WITH user_data AS (
        SELECT u.id, u.hospital_id, u.name, u.email, u.is_active, u.password_hash
        FROM users u
        WHERE u.email = p_identifier OR u.id::TEXT = p_identifier
    ),
    user_role_names AS (
        SELECT ur.user_id, jsonb_agg(DISTINCT r.name) AS roles
        FROM user_roles ur
        JOIN roles r ON ur.role_id = r.id
        WHERE ur.user_id = (SELECT id FROM user_data)
        GROUP BY ur.user_id
    ),
    role_perms AS (
        SELECT p.code, rp.constraints
        FROM user_roles ur
        JOIN role_permissions rp ON ur.role_id = rp.role_id
        JOIN permissions p ON rp.permission_id = p.id
        WHERE ur.user_id = (SELECT id FROM user_data)
    ),
    grant_overrides AS (
        SELECT p.code, upo.constraints
        FROM user_permission_overrides upo
        JOIN permissions p ON upo.permission_id = p.id
        WHERE upo.user_id = (SELECT id FROM user_data)
          AND upo.effect = 'grant'
    ),
    deny_overrides AS (
        SELECT p.code
        FROM user_permission_overrides upo
        JOIN permissions p ON upo.permission_id = p.id
        WHERE upo.user_id = (SELECT id FROM user_data)
          AND upo.effect IN ('deny', 'revoke')
    ),
    combined_perms AS (
        SELECT code, constraints FROM role_perms
        UNION ALL
        SELECT code, constraints FROM grant_overrides
    ),
    final_perms AS (
        SELECT code, constraints
        FROM combined_perms
        WHERE code NOT IN (SELECT code FROM deny_overrides)
    )
    SELECT 
        ud.id AS user_id,
        ud.hospital_id,
        ud.name,
        ud.email,
        ud.is_active,
        ud.password_hash,
        COALESCE(urn.roles, '[]'::jsonb) AS roles,
        COALESCE(
            (SELECT jsonb_object_agg(code, constraints) FROM final_perms),
            '{}'::jsonb
        ) AS effective_permissions
    FROM user_data ud
    LEFT JOIN user_role_names urn ON ud.id = urn.user_id;
END;
$$;
