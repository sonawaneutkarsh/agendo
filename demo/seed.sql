-- ESP32 test-bench schema + seed data + hardware push log table for Agendo demo

CREATE TABLE IF NOT EXISTS esp32_devices (
    board_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    firmware_status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pin_configs (
    board_id TEXT NOT NULL REFERENCES esp32_devices(board_id) ON DELETE CASCADE,
    gpio_pin INT NOT NULL,
    mode TEXT NOT NULL,
    voltage_mv INT NOT NULL,
    pull_up BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (board_id, gpio_pin)
);

CREATE TABLE IF NOT EXISTS diag_logs (
    id SERIAL PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES esp32_devices(board_id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calibration (
    board_id TEXT NOT NULL REFERENCES esp32_devices(board_id) ON DELETE CASCADE,
    sensor TEXT NOT NULL,
    offset_mv INT NOT NULL,
    PRIMARY KEY (board_id, sensor)
);

-- Persisted mock hardware push log (replaces in-memory Python list)
CREATE TABLE IF NOT EXISTS agendo_hardware_pushes (
    id BIGSERIAL PRIMARY KEY,
    action_id UUID NOT NULL REFERENCES agendo_actions(action_id) ON DELETE CASCADE,
    board_id TEXT NOT NULL,
    transport TEXT NOT NULL,
    pins_pushed JSONB NOT NULL,
    pushed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Attach Agendo capture triggers via attach_agendo_trigger() helper
SELECT attach_agendo_trigger('esp32_devices');
SELECT attach_agendo_trigger('pin_configs');
SELECT attach_agendo_trigger('diag_logs');
SELECT attach_agendo_trigger('calibration');

-- Seed initial state with capture temporarily suppressed so seed data isn't logged as untracked writes
DO $$
BEGIN
    PERFORM set_config('agendo.is_restoring', 'true', true);

    INSERT INTO esp32_devices (board_id, name, firmware_status)
    VALUES ('esp32-bench-01', 'ESP32-S3 Power & I2C Bench', 'healthy')
    ON CONFLICT (board_id) DO NOTHING;

    INSERT INTO pin_configs (board_id, gpio_pin, mode, voltage_mv, pull_up)
    VALUES
        ('esp32-bench-01', 4,  'I2C_SDA',    3300, true),
        ('esp32-bench-01', 5,  'I2C_SCL',    3300, true),
        ('esp32-bench-01', 12, 'ADC_IN',     1800, false),
        ('esp32-bench-01', 13, 'STATUS_LED', 3300, false)
    ON CONFLICT (board_id, gpio_pin) DO NOTHING;

    INSERT INTO calibration (board_id, sensor, offset_mv)
    VALUES
        ('esp32-bench-01', 'vbat_adc', -12),
        ('esp32-bench-01', 'temp_ntc', 8)
    ON CONFLICT (board_id, sensor) DO NOTHING;

    INSERT INTO diag_logs (board_id, message)
    VALUES
        ('esp32-bench-01', 'Initial bench boot OK. I2C bus nominal, ADC_IN=1800mV.');
END $$;
