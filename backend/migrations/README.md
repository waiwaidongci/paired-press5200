# Schema 版本说明

## v1（初始）
由 SQLAlchemy 模型定义，启动时通过 `Base.metadata.create_all` 建表。本地小型应用不执行破坏性迁移；调整 schema 前请备份命名卷 db-data。

## v2（研究包迁入迁出）
新增表 `import_batches`，仍由启动时 `create_all` 以非破坏方式创建，不改动既有 `studies / observations / runs / reviews / audits / users` 表结构。

字段：
- `source_station` + `source_study_id`：同源幂等键，同一来源研究的重复/重试迁入不新增项目。
- `package_checksum`：迁出清单 SHA256；同源再次迁入但校验值不同即判定内容变动并拒绝，原项不变。
- `status`：`staging`（中断可恢复，映射已落库）/ `completed` / `failed`。
- `mapping`：来源编号 → 本地编号的完整映射（study/observation/run/review/artifact），中断恢复与并发迁入沿用同一份。
- `conflict_note` / `error` / 时间戳：同名异源后缀、失败原因与过程时间。

约束取舍：`studies.name` 的唯一约束保持不变；同名异源研究在迁入时追加来源站点后缀并存，原始名称与来源站点记录在 `mapping` 与留档中。
