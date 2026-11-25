-- Agendo core schema, generic capture trigger, and trigger attachment helper (Postgres 16)

CREATE SEQUENCE IF NOT EXISTS agendo_diff_seq AS BIGINT;

CREATE TABLE IF NOT EXISTS agendo_actions (
    action_id UUID PRIMARY KEY,
    agent_name TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'applied'
        CHECK (status IN ('applied', 'reverted', 'conflict')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agendo_row_diffs (
    id BIGSERIAL PRIMARY KEY,
    -- Nullable on purpose: raw/untracked writes outside the SDK have action_id = NULL
    action_id UUID REFERENCES agendo_actions(action_id) ON DELETE CASCADE,
    seq BIGINT NOT NULL DEFAULT nextval('agendo_diff_seq'),
    table_name TEXT NOT NULL,
    pk JSONB NOT NULL,
    op TEXT NOT NULL CHECK (op IN ('INSERT', 'UPDATE', 'DELETE')),
    old_row JSONB,
    new_row JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agendo_row_diffs_action_seq
    ON agendo_row_diffs (action_id, seq DESC);

CREATE INDEX IF NOT EXISTS idx_agendo_row_diffs_untracked
    ON agendo_row_diffs (created_at DESC)
    WHERE action_id IS NULL;

-- Generic row-level capture trigger.
-- Pass primary key column name(s) as trigger arguments:
--   EXECUTE FUNCTION agendo_capture('board_id', 'gpio_pin');
CREATE OR REPLACE FUNCTION agendo_capture()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_is_restoring TEXT;
    v_action_id_raw TEXT;
    v_action_id UUID;
    v_old_json JSONB;
    v_new_json JSONB;
    v_pk_source JSONB;
    v_pk JSONB := '{}'::jsonb;
    v_col TEXT;
    i INT;
BEGIN
    -- Skip capture during restore so compensating SQL does not record a new mutation
    v_is_restoring := current_setting('agendo.is_restoring', true);
    IF v_is_restoring = 'true' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    v_action_id_raw := NULLIF(current_setting('agendo.action_id', true), '');
    IF v_action_id_raw IS NOT NULL THEN
        v_action_id := v_action_id_raw::uuid;
    ELSE
        v_action_id := NULL;
    END IF;

    IF TG_OP = 'INSERT' THEN
        v_old_json := NULL;
        v_new_json := to_jsonb(NEW);
        v_pk_source := v_new_json;
    ELSIF TG_OP = 'UPDATE' THEN
        v_old_json := to_jsonb(OLD);
        v_new_json := to_jsonb(NEW);
        v_pk_source := v_old_json;
    ELSIF TG_OP = 'DELETE' THEN
        v_old_json := to_jsonb(OLD);
        v_new_json := NULL;
        v_pk_source := v_old_json;
    END IF;

    IF TG_NARGS = 0 THEN
        RAISE EXCEPTION 'agendo_capture() requires primary key column name(s) as trigger arguments on table %', TG_TABLE_NAME;
    END IF;

    FOR i IN 0 .. TG_NARGS - 1 LOOP
        v_col := TG_ARGV[i];
        v_pk := v_pk || jsonb_build_object(v_col, v_pk_source -> v_col);
    END LOOP;

    INSERT INTO agendo_row_diffs (action_id, table_name, pk, op, old_row, new_row)
    VALUES (v_action_id, TG_TABLE_NAME, v_pk, TG_OP, v_old_json, v_new_json);

    RETURN COALESCE(NEW, OLD);
END;
$$;

-- Helper to attach agendo_capture() to any table by inspecting its PRIMARY KEY columns.
-- Usage: SELECT attach_agendo_trigger('orders');
CREATE OR REPLACE FUNCTION attach_agendo_trigger(target_table REGCLASS)
RETURNS TEXT
LANGUAGE plpgsql
AS $$
DECLARE
    v_schema TEXT;
    v_table TEXT;
    v_pk_cols TEXT[];
    v_args TEXT;
    v_trg_name TEXT;
BEGIN
    SELECT n.nspname, c.relname
    INTO v_schema, v_table
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.oid = target_table;

    IF v_table IS NULL THEN
        RAISE EXCEPTION 'Table % does not exist', target_table;
    END IF;

    SELECT array_agg(a.attname ORDER BY u.ord)
    INTO v_pk_cols
    FROM pg_index i
    JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS u(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = u.attnum
    WHERE i.indrelid = target_table
      AND i.indisprimary;

    IF v_pk_cols IS NULL OR array_length(v_pk_cols, 1) IS NULL THEN
        RAISE EXCEPTION 'Table %.% must have a PRIMARY KEY to attach agendo_capture()', v_schema, v_table;
    END IF;

    SELECT string_agg(quote_literal(col), ', ')
    INTO v_args
    FROM unnest(v_pk_cols) AS col;

    v_trg_name := 'agendo_trg_' || v_table;

    EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I.%I', v_trg_name, v_schema, v_table);
    EXECUTE format(
        'CREATE TRIGGER %I AFTER INSERT OR UPDATE OR DELETE ON %I.%I FOR EACH ROW EXECUTE FUNCTION agendo_capture(%s)',
        v_trg_name,
        v_schema,
        v_table,
        v_args
    );

    RETURN format('%I.%I (%s)', v_schema, v_table, array_to_string(v_pk_cols, ', '));
END;
$$;
