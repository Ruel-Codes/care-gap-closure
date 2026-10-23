-- Patient cost and care-gap analytics
-- Run in MySQL Workbench on Local instance 3306, schema healthcare_analytics.
-- Staging tables (stg_*) are loaded from the Synthea CSVs by
-- python/build_healthcare.py. This script cleans them and builds the views.
--
-- Encounter cost is base encounter cost + procedure cost + medication cost.
-- TOTAL_CLAIM_COST is only $129.16 or $77.49, so it is kept but not used
-- for the High / Mid / Low bands.

USE healthcare_analytics;
SET NAMES utf8mb4;

DROP VIEW IF EXISTS v_monthly_volume;
DROP VIEW IF EXISTS v_repeat_visit_years;
DROP VIEW IF EXISTS v_care_gaps;
DROP VIEW IF EXISTS v_condition_rank;
DROP VIEW IF EXISTS v_patient_cost_rank;
DROP VIEW IF EXISTS v_encounter_economics;

DROP TABLE IF EXISTS data_quality_log;
DROP TABLE IF EXISTS medications;
DROP TABLE IF EXISTS procedures;
DROP TABLE IF EXISTS conditions;
DROP TABLE IF EXISTS encounters;
DROP TABLE IF EXISTS providers;
DROP TABLE IF EXISTS payers;
DROP TABLE IF EXISTS patients;
DROP TABLE IF EXISTS procedure_date_work;

CREATE TABLE patients (
  id CHAR(36) NOT NULL,
  birthdate DATE NOT NULL,
  deathdate DATE NULL,
  ssn VARCHAR(16) NULL,
  first_name VARCHAR(80) NOT NULL,
  last_name VARCHAR(80) NOT NULL,
  marital VARCHAR(20) NULL,
  race VARCHAR(40) NOT NULL,
  ethnicity VARCHAR(40) NOT NULL,
  gender CHAR(1) NOT NULL,
  birthplace VARCHAR(120) NULL,
  address VARCHAR(180) NULL,
  city VARCHAR(80) NULL,
  state VARCHAR(40) NOT NULL,
  county VARCHAR(80) NULL,
  zip VARCHAR(10) NULL,
  lat DECIMAL(9, 6) NULL,
  lon DECIMAL(9, 6) NULL,
  healthcare_expenses DECIMAL(14, 2) NOT NULL,
  healthcare_coverage DECIMAL(14, 2) NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB;

CREATE TABLE payers (
  id CHAR(36) NOT NULL,
  name VARCHAR(120) NOT NULL,
  state_headquartered VARCHAR(40) NULL,
  amount_covered DECIMAL(16, 2) NOT NULL,
  amount_uncovered DECIMAL(16, 2) NOT NULL,
  revenue DECIMAL(16, 2) NOT NULL,
  unique_customers INT NOT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB;

CREATE TABLE providers (
  id CHAR(36) NOT NULL,
  organization_id CHAR(36) NULL,
  name VARCHAR(160) NOT NULL,
  gender CHAR(1) NULL,
  speciality VARCHAR(80) NULL,
  state VARCHAR(40) NULL,
  utilization INT NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB;

CREATE TABLE encounters (
  id CHAR(36) NOT NULL,
  start_ts DATETIME NOT NULL,
  stop_ts DATETIME NOT NULL,
  patient_id CHAR(36) NOT NULL,
  organization_id CHAR(36) NULL,
  provider_id CHAR(36) NOT NULL,
  payer_id CHAR(36) NOT NULL,
  encounter_class VARCHAR(32) NOT NULL,
  code VARCHAR(32) NOT NULL,
  description VARCHAR(180) NOT NULL,
  base_encounter_cost DECIMAL(14, 2) NOT NULL,
  total_claim_cost DECIMAL(14, 2) NOT NULL,
  payer_coverage DECIMAL(14, 2) NOT NULL,
  reason_code VARCHAR(32) NULL,
  reason_description VARCHAR(180) NULL,
  after_death TINYINT NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  KEY ix_enc_patient (patient_id),
  KEY ix_enc_start (start_ts),
  KEY ix_enc_class (encounter_class),
  KEY ix_enc_payer (payer_id),
  CONSTRAINT fk_enc_patient FOREIGN KEY (patient_id) REFERENCES patients (id),
  CONSTRAINT fk_enc_payer FOREIGN KEY (payer_id) REFERENCES payers (id),
  CONSTRAINT fk_enc_provider FOREIGN KEY (provider_id) REFERENCES providers (id)
) ENGINE=InnoDB;

CREATE TABLE conditions (
  id INT NOT NULL AUTO_INCREMENT,
  start_date DATE NOT NULL,
  stop_date DATE NULL,
  patient_id CHAR(36) NOT NULL,
  encounter_id CHAR(36) NOT NULL,
  code VARCHAR(32) NOT NULL,
  description VARCHAR(180) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_cond_patient (patient_id),
  KEY ix_cond_encounter (encounter_id),
  CONSTRAINT fk_cond_patient FOREIGN KEY (patient_id) REFERENCES patients (id),
  CONSTRAINT fk_cond_encounter FOREIGN KEY (encounter_id) REFERENCES encounters (id)
) ENGINE=InnoDB;

CREATE TABLE procedures (
  id INT NOT NULL AUTO_INCREMENT,
  procedure_date DATETIME NOT NULL,
  procedure_date_original DATETIME NOT NULL,
  date_was_aligned TINYINT NOT NULL,
  patient_id CHAR(36) NOT NULL,
  encounter_id CHAR(36) NOT NULL,
  code VARCHAR(32) NOT NULL,
  description VARCHAR(180) NOT NULL,
  base_cost DECIMAL(14, 2) NOT NULL,
  reason_code VARCHAR(32) NULL,
  reason_description VARCHAR(180) NULL,
  PRIMARY KEY (id),
  KEY ix_proc_encounter (encounter_id),
  KEY ix_proc_patient (patient_id),
  CONSTRAINT fk_proc_patient FOREIGN KEY (patient_id) REFERENCES patients (id),
  CONSTRAINT fk_proc_encounter FOREIGN KEY (encounter_id) REFERENCES encounters (id)
) ENGINE=InnoDB;

CREATE TABLE medications (
  id INT NOT NULL AUTO_INCREMENT,
  start_ts DATETIME NOT NULL,
  stop_ts DATETIME NULL,
  patient_id CHAR(36) NOT NULL,
  payer_id CHAR(36) NOT NULL,
  encounter_id CHAR(36) NOT NULL,
  code VARCHAR(32) NOT NULL,
  description VARCHAR(180) NOT NULL,
  base_cost DECIMAL(14, 2) NOT NULL,
  payer_coverage DECIMAL(14, 2) NOT NULL,
  dispenses INT NOT NULL,
  total_cost DECIMAL(14, 2) NOT NULL,
  reason_code VARCHAR(32) NULL,
  reason_description VARCHAR(180) NULL,
  PRIMARY KEY (id),
  KEY ix_med_encounter (encounter_id),
  KEY ix_med_patient (patient_id),
  CONSTRAINT fk_med_patient FOREIGN KEY (patient_id) REFERENCES patients (id),
  CONSTRAINT fk_med_payer FOREIGN KEY (payer_id) REFERENCES payers (id),
  CONSTRAINT fk_med_encounter FOREIGN KEY (encounter_id) REFERENCES encounters (id)
) ENGINE=InnoDB;

CREATE TABLE data_quality_log (
  id INT NOT NULL AUTO_INCREMENT,
  issue_type VARCHAR(64) NOT NULL,
  table_name VARCHAR(64) NOT NULL,
  column_name VARCHAR(64) NOT NULL,
  row_key VARCHAR(220) NOT NULL,
  action VARCHAR(40) NOT NULL,
  detail VARCHAR(400) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_dq_action (action)
) ENGINE=InnoDB;

INSERT INTO payers (id, name, state_headquartered, amount_covered, amount_uncovered, revenue, unique_customers)
SELECT
  id,
  name,
  NULLIF(state_headquartered, ''),
  CAST(amount_covered AS DECIMAL(16, 2)),
  CAST(amount_uncovered AS DECIMAL(16, 2)),
  CAST(revenue AS DECIMAL(16, 2)),
  CAST(unique_customers AS UNSIGNED)
FROM stg_payers;

INSERT INTO providers (id, organization_id, name, gender, speciality, state, utilization)
SELECT
  id,
  NULLIF(organization, ''),
  name,
  NULLIF(gender, ''),
  NULLIF(speciality, ''),
  NULLIF(state, ''),
  CAST(NULLIF(utilization, '') AS UNSIGNED)
FROM stg_providers;

INSERT INTO data_quality_log (issue_type, table_name, column_name, row_key, action, detail)
SELECT
  'duplicate_ssn',
  'patients',
  'ssn',
  id,
  'cleared_ssn',
  CONCAT('SSN ', ssn, ' is shared by two different patients and was cleared')
FROM stg_patients
WHERE ssn <> ''
  AND ssn IN (
    SELECT ssn
    FROM (
      SELECT ssn
      FROM stg_patients
      WHERE ssn <> ''
      GROUP BY ssn
      HAVING COUNT(*) > 1
    ) shared
  );

INSERT INTO patients (
  id, birthdate, deathdate, ssn, first_name, last_name, marital, race, ethnicity,
  gender, birthplace, address, city, state, county, zip, lat, lon,
  healthcare_expenses, healthcare_coverage
)
SELECT
  id,
  STR_TO_DATE(birthdate, '%Y-%m-%d'),
  STR_TO_DATE(NULLIF(deathdate, ''), '%Y-%m-%d'),
  CASE
    WHEN ssn = '' THEN NULL
    WHEN ssn IN (
      SELECT ssn FROM (
        SELECT ssn FROM stg_patients WHERE ssn <> '' GROUP BY ssn HAVING COUNT(*) > 1
      ) shared
    ) THEN NULL
    ELSE ssn
  END,
  first,
  last,
  NULLIF(marital, ''),
  race,
  ethnicity,
  gender,
  NULLIF(birthplace, ''),
  NULLIF(address, ''),
  NULLIF(city, ''),
  state,
  NULLIF(county, ''),
  NULLIF(zip, ''),
  CAST(NULLIF(lat, '') AS DECIMAL(9, 6)),
  CAST(NULLIF(lon, '') AS DECIMAL(9, 6)),
  CAST(healthcare_expenses AS DECIMAL(14, 2)),
  CAST(healthcare_coverage AS DECIMAL(14, 2))
FROM stg_patients;

INSERT INTO data_quality_log (issue_type, table_name, column_name, row_key, action, detail)
SELECT
  'missing_zip',
  'patients',
  'zip',
  id,
  'flagged_not_imputed',
  'ZIP is blank; left as NULL because the source has no replacement value'
FROM stg_patients
WHERE zip = '';

INSERT INTO encounters (
  id, start_ts, stop_ts, patient_id, organization_id, provider_id, payer_id,
  encounter_class, code, description, base_encounter_cost, total_claim_cost,
  payer_coverage, reason_code, reason_description, after_death
)
SELECT
  e.id,
  CASE
    WHEN e.start LIKE '%T%' THEN STR_TO_DATE(REPLACE(e.start, 'Z', ''), '%Y-%m-%dT%H:%i:%s')
    ELSE STR_TO_DATE(e.start, '%Y-%m-%d')
  END,
  CASE
    WHEN e.stop LIKE '%T%' THEN STR_TO_DATE(REPLACE(e.stop, 'Z', ''), '%Y-%m-%dT%H:%i:%s')
    ELSE STR_TO_DATE(e.stop, '%Y-%m-%d')
  END,
  e.patient,
  NULLIF(e.organization, ''),
  e.provider,
  e.payer,
  e.encounterclass,
  e.code,
  e.description,
  CAST(e.base_encounter_cost AS DECIMAL(14, 2)),
  CAST(e.total_claim_cost AS DECIMAL(14, 2)),
  CAST(e.payer_coverage AS DECIMAL(14, 2)),
  NULLIF(e.reasoncode, ''),
  NULLIF(e.reasondescription, ''),
  CASE
    WHEN p.deathdate IS NOT NULL
     AND DATE(CASE
            WHEN e.start LIKE '%T%' THEN STR_TO_DATE(REPLACE(e.start, 'Z', ''), '%Y-%m-%dT%H:%i:%s')
            ELSE STR_TO_DATE(e.start, '%Y-%m-%d')
          END) > p.deathdate
    THEN 1 ELSE 0
  END
FROM stg_encounters e
JOIN patients p ON p.id = e.patient;

INSERT INTO data_quality_log (issue_type, table_name, column_name, row_key, action, detail)
SELECT
  'encounter_after_death',
  'encounters',
  'start_ts',
  id,
  'flagged_retained',
  'Encounter date is after the patient death date; row kept and flagged'
FROM encounters
WHERE after_death = 1;

INSERT INTO data_quality_log (issue_type, table_name, column_name, row_key, action, detail)
SELECT
  'missing_end_date',
  'conditions',
  'stop_date',
  CONCAT(patient, '|', encounter, '|', code),
  'blank_to_null',
  'Empty STOP stored as NULL so the condition stays open and date checks work'
FROM stg_conditions
WHERE stop = '';

INSERT INTO conditions (start_date, stop_date, patient_id, encounter_id, code, description)
SELECT
  STR_TO_DATE(start, '%Y-%m-%d'),
  STR_TO_DATE(NULLIF(stop, ''), '%Y-%m-%d'),
  patient,
  encounter,
  code,
  description
FROM stg_conditions;

CREATE TABLE procedure_date_work (
  patient_id CHAR(36) NOT NULL,
  encounter_id CHAR(36) NOT NULL,
  code VARCHAR(32) NOT NULL,
  description VARCHAR(180) NOT NULL,
  base_cost DECIMAL(14, 2) NOT NULL,
  reason_code VARCHAR(32) NULL,
  reason_description VARCHAR(180) NULL,
  procedure_dt DATETIME NOT NULL,
  encounter_start DATETIME NOT NULL,
  encounter_stop DATETIME NOT NULL,
  aligned TINYINT NOT NULL,
  KEY ix_work_aligned (aligned)
) ENGINE=InnoDB;

INSERT INTO procedure_date_work (
  patient_id, encounter_id, code, description, base_cost, reason_code, reason_description,
  procedure_dt, encounter_start, encounter_stop, aligned
)
SELECT
  s.patient,
  s.encounter,
  s.code,
  s.description,
  CAST(s.base_cost AS DECIMAL(14, 2)),
  NULLIF(s.reasoncode, ''),
  NULLIF(s.reasondescription, ''),
  CASE
    WHEN s.date LIKE '%T%' THEN STR_TO_DATE(REPLACE(s.date, 'Z', ''), '%Y-%m-%dT%H:%i:%s')
    ELSE STR_TO_DATE(s.date, '%Y-%m-%d')
  END,
  e.start_ts,
  e.stop_ts,
  0
FROM stg_procedures s
JOIN encounters e ON e.id = s.encounter;

UPDATE procedure_date_work
SET aligned = 1
WHERE DATE(procedure_dt) < DATE(encounter_start)
   OR DATE(procedure_dt) > DATE(encounter_stop);

INSERT INTO data_quality_log (issue_type, table_name, column_name, row_key, action, detail)
SELECT
  'procedure_date_outside_encounter',
  'procedures',
  'procedure_date',
  CONCAT(encounter_id, '|', code, '|', DATE_FORMAT(procedure_dt, '%Y-%m-%d')),
  'aligned_procedure_date',
  CONCAT(
    'Procedure date ', DATE_FORMAT(procedure_dt, '%Y-%m-%d'),
    ' fell outside the encounter and was aligned to ',
    DATE_FORMAT(encounter_start, '%Y-%m-%d %H:%i:%s')
  )
FROM procedure_date_work
WHERE aligned = 1;

INSERT INTO procedures (
  procedure_date, procedure_date_original, date_was_aligned, patient_id, encounter_id,
  code, description, base_cost, reason_code, reason_description
)
SELECT
  IF(aligned = 1, encounter_start, procedure_dt),
  procedure_dt,
  aligned,
  patient_id,
  encounter_id,
  code,
  description,
  base_cost,
  reason_code,
  reason_description
FROM procedure_date_work;

DROP TABLE procedure_date_work;

INSERT INTO data_quality_log (issue_type, table_name, column_name, row_key, action, detail)
SELECT
  'missing_end_date',
  'medications',
  'stop_ts',
  CONCAT(patient, '|', encounter, '|', code, '|', start),
  'blank_to_null',
  'Empty STOP stored as NULL so the medication stays open and date checks work'
FROM stg_medications
WHERE stop = '';

INSERT INTO medications (
  start_ts, stop_ts, patient_id, payer_id, encounter_id, code, description,
  base_cost, payer_coverage, dispenses, total_cost, reason_code, reason_description
)
SELECT
  CASE
    WHEN start LIKE '%T%' THEN STR_TO_DATE(REPLACE(start, 'Z', ''), '%Y-%m-%dT%H:%i:%s')
    ELSE STR_TO_DATE(start, '%Y-%m-%d')
  END,
  CASE
    WHEN stop = '' THEN NULL
    WHEN stop LIKE '%T%' THEN STR_TO_DATE(REPLACE(stop, 'Z', ''), '%Y-%m-%dT%H:%i:%s')
    ELSE STR_TO_DATE(stop, '%Y-%m-%d')
  END,
  patient,
  payer,
  encounter,
  code,
  description,
  CAST(base_cost AS DECIMAL(14, 2)),
  CAST(payer_coverage AS DECIMAL(14, 2)),
  CAST(dispenses AS UNSIGNED),
  CAST(totalcost AS DECIMAL(14, 2)),
  NULLIF(reasoncode, ''),
  NULLIF(reasondescription, '')
FROM stg_medications;

CREATE VIEW v_encounter_economics AS
SELECT
  e.id AS encounter_id,
  e.patient_id,
  e.encounter_class,
  e.start_ts,
  e.stop_ts,
  e.payer_id,
  e.provider_id,
  e.description AS encounter_description,
  e.base_encounter_cost,
  e.total_claim_cost,
  e.payer_coverage,
  e.after_death,
  COALESCE(p.procedure_cost, 0) AS procedure_cost,
  COALESCE(m.medication_cost, 0) AS medication_cost,
  e.base_encounter_cost + COALESCE(p.procedure_cost, 0) + COALESCE(m.medication_cost, 0) AS total_cost,
  CASE
    WHEN e.base_encounter_cost + COALESCE(p.procedure_cost, 0) + COALESCE(m.medication_cost, 0) >= 10000 THEN 'High'
    WHEN e.base_encounter_cost + COALESCE(p.procedure_cost, 0) + COALESCE(m.medication_cost, 0) >= 500 THEN 'Mid'
    ELSE 'Low'
  END AS cost_band
FROM encounters e
LEFT JOIN (
  SELECT encounter_id, SUM(base_cost) AS procedure_cost
  FROM procedures
  GROUP BY encounter_id
) p ON p.encounter_id = e.id
LEFT JOIN (
  SELECT encounter_id, SUM(total_cost) AS medication_cost
  FROM medications
  GROUP BY encounter_id
) m ON m.encounter_id = e.id;

CREATE VIEW v_patient_cost_rank AS
SELECT
  patient_id,
  patient_cost,
  encounters,
  DENSE_RANK() OVER (ORDER BY patient_cost DESC) AS cost_rank
FROM (
  SELECT patient_id, SUM(total_cost) AS patient_cost, COUNT(*) AS encounters
  FROM v_encounter_economics
  GROUP BY patient_id
) ranked;

CREATE VIEW v_condition_rank AS
SELECT
  encounter_class,
  condition_description,
  condition_count,
  attributed_cost,
  DENSE_RANK() OVER (
    PARTITION BY encounter_class
    ORDER BY attributed_cost DESC, condition_count DESC
  ) AS cost_rank
FROM (
  SELECT
    e.encounter_class,
    c.description AS condition_description,
    COUNT(*) AS condition_count,
    SUM(e.total_cost) AS attributed_cost
  FROM conditions c
  JOIN v_encounter_economics e ON e.encounter_id = c.encounter_id
  GROUP BY e.encounter_class, c.description
) ranked;

CREATE VIEW v_care_gaps AS
SELECT
  c.patient_id,
  c.encounter_id,
  c.code AS condition_code,
  c.description AS condition_description,
  c.start_date
FROM conditions c
LEFT JOIN procedures p ON p.encounter_id = c.encounter_id
LEFT JOIN medications m ON m.encounter_id = c.encounter_id
WHERE p.encounter_id IS NULL
  AND m.encounter_id IS NULL;

CREATE VIEW v_repeat_visit_years AS
SELECT
  patient_id,
  YEAR(start_ts) AS visit_year,
  COUNT(*) AS encounters
FROM encounters
GROUP BY patient_id, YEAR(start_ts)
HAVING COUNT(*) > 1;

CREATE VIEW v_monthly_volume AS
SELECT
  month_start,
  encounters,
  encounters - LAG(encounters) OVER (ORDER BY month_start) AS mom_change,
  ROUND(
    100 * (encounters - LAG(encounters) OVER (ORDER BY month_start))
      / LAG(encounters) OVER (ORDER BY month_start),
    1
  ) AS mom_pct
FROM (
  SELECT DATE_FORMAT(start_ts, '%Y-%m-01') AS month_start, COUNT(*) AS encounters
  FROM encounters
  GROUP BY DATE_FORMAT(start_ts, '%Y-%m-01')
) monthly;

-- Workbench result sets
SELECT action, COUNT(*) AS issues
FROM data_quality_log
GROUP BY action
ORDER BY issues DESC;

SELECT cost_band, COUNT(*) AS encounters, ROUND(SUM(total_cost), 2) AS total_cost
FROM v_encounter_economics
GROUP BY cost_band
ORDER BY FIELD(cost_band, 'Low', 'Mid', 'High');

SELECT COUNT(*) AS gap_events, COUNT(DISTINCT patient_id) AS gap_patients
FROM v_care_gaps;

SELECT COUNT(*) AS repeat_patient_years, COUNT(DISTINCT patient_id) AS repeat_patients
FROM v_repeat_visit_years;
