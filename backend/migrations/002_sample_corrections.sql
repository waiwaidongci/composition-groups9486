-- 批量复测更正批次表；表结构同样由 SQLAlchemy metadata 首次启动创建。
-- 显式 DDL 供既有环境核对，索引支撑按研究查询与并发确认时的批次筛选。
CREATE TABLE IF NOT EXISTS sample_corrections (
  id BIGINT NOT NULL AUTO_INCREMENT,
  study_id BIGINT NOT NULL,
  reason TEXT NOT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'preview',
  items JSON NOT NULL,
  conflict JSON,
  created_by VARCHAR(160) NOT NULL,
  confirmed_by VARCHAR(160) NOT NULL DEFAULT '',
  created_at VARCHAR(64) NOT NULL,
  confirmed_at VARCHAR(64) NOT NULL DEFAULT '',
  PRIMARY KEY (id),
  CONSTRAINT fk_correction_study FOREIGN KEY (study_id) REFERENCES composition_studies (id)
);
CREATE INDEX correction_study_status_idx ON sample_corrections (study_id, status);
