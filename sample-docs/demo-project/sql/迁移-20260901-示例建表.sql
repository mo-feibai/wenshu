-- @meta
-- title: 示例建表迁移
-- project: demo-project
-- domain: 示例域
-- topic: 示例建表
-- type: sql
-- status: executed
-- version: 1
-- date: 2026-09-01
-- related: demo-project/api/示例接口文档
-- @end
CREATE TABLE `demo_item` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `name` VARCHAR(64) NOT NULL COMMENT '名称',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_name` (`name`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '示例条目表';
