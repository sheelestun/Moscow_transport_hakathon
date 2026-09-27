-- History of the live system, applied idempotently at startup.
-- Dataset times are `timestamp` (naive Moscow-local, as in the dataset); wall-clock times are `timestamptz`.

CREATE TABLE IF NOT EXISTS telemetry (
    tr_id           integer          NOT NULL,
    unit_id         bigint           NOT NULL,
    event_time      timestamp        NOT NULL,
    lat             double precision,
    lon             double precision,
    speed_kmh       real,
    heading_deg     real,
    location_valid  boolean          NOT NULL,
    is_hist         boolean          NOT NULL,
    source          text             NOT NULL,   -- ndtp | replay
    received_at     timestamptz      NOT NULL
);
CREATE INDEX IF NOT EXISTS telemetry_tr_time ON telemetry (tr_id, event_time);

-- GPS-detected arrivals at planned stops; re-detection updates the row.
CREATE TABLE IF NOT EXISTS arrivals (
    tr_id        integer     NOT NULL,
    stop_id      bigint      NOT NULL,        -- tt_action_item_id (one planned visit)
    stop_name    text,
    plan         timestamp   NOT NULL,
    arrival      timestamp   NOT NULL,
    delay_s      real        NOT NULL,
    dwell_s      real        NOT NULL,
    trip         integer     NOT NULL,
    detected_at  timestamptz NOT NULL,
    PRIMARY KEY (tr_id, stop_id)
);

CREATE TABLE IF NOT EXISTS predictions (
    id              bigserial   PRIMARY KEY,
    sample_id       text        NOT NULL,
    tr_id           integer     NOT NULL,
    t               timestamp   NOT NULL,     -- dataset time the forecast was made at
    target_stop_id  bigint      NOT NULL,
    target_plan     timestamp   NOT NULL,
    lead_s          real        NOT NULL,
    horizon_ok      boolean     NOT NULL,
    cur_dev_s       real,
    delay_pred_s    real        NOT NULL,
    risk_score      real        NOT NULL,
    risk_level      text        NOT NULL,
    confidence      real,
    source          text        NOT NULL,     -- ml | fallback
    model_version   text,
    data_status     text,
    reason_pattern  text,
    response        jsonb       NOT NULL,     -- the full ML response
    made_at         timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS predictions_tr_t ON predictions (tr_id, t);
CREATE INDEX IF NOT EXISTS predictions_target ON predictions (target_stop_id);

-- One row per alert; status moves active -> verified | resolved.
CREATE TABLE IF NOT EXISTS alerts (
    alert_id        text        PRIMARY KEY,
    tr_id           integer     NOT NULL,
    sample_id       text        NOT NULL,
    target_stop_id  bigint      NOT NULL,
    target_plan     timestamp   NOT NULL,
    segment_from    text,
    segment_to      text,
    delay_pred_s    real        NOT NULL,
    risk_score      real        NOT NULL,
    status          text        NOT NULL,
    delay_fact_s    real,
    hit             boolean,
    resolution      text,
    created_at      timestamptz NOT NULL,
    closed_at       timestamptz
);
CREATE INDEX IF NOT EXISTS alerts_created ON alerts (created_at);
