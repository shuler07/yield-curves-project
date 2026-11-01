INSERT INTO curve_points (id, curve_date, quote_time, tenor, yield_rate, discount_factor,
etl_run_id, created_at, is_interpolated, is_extrapolated, is_deleted)
SELECT id, curve_date, quote_time, tenor, yield_rate, discount_factor,
etl_run_id, created_at, is_interpolated, is_extrapolated, is_deleted
FROM processed_curve_points WHERE scenario = 'Baseline';
-- statement-break
INSERT INTO stress_curves (id, curve_point_id, stress_shock_id, stressed_yield,
discount_factor, etl_run_id, created_at)
SELECT c.id, p.id, s.id, c.yield_rate, c.discount_factor, c.etl_run_id, c.created_at
FROM processed_curve_points c
JOIN curve_points p ON p.curve_date = c.curve_date
AND p.quote_time = c.quote_time AND p.tenor = c.tenor
JOIN stress_shocks s ON s.scenario = c.scenario AND s.version = c.scenario_version;
-- statement-break
SELECT setval(pg_get_serial_sequence('curve_points', 'id'),
COALESCE((SELECT max(id) FROM curve_points), 1), EXISTS (SELECT 1 FROM curve_points));
-- statement-break
SELECT setval(pg_get_serial_sequence('stress_curves', 'id'),
COALESCE((SELECT max(id) FROM stress_curves), 1), EXISTS (SELECT 1 FROM stress_curves));
-- statement-break
DROP TABLE processed_curve_points;
-- statement-break
DROP TABLE raw_curve_points;
