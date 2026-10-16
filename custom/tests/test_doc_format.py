# -*- coding: utf-8 -*-
"""规范骨架 lint 与脚手架单元测试（纯函数，不连服务、不写文档库）。"""
import importlib.util
import pathlib
import unittest


SERVER_PATH = pathlib.Path(__file__).resolve().parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("wenshu_doc_format_test", SERVER_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


CONFORMING = """---
title: "示例接口文档（v1）"
project: "demo"
topic: "示例"
type: "api"
status: "pending"
date: "2026-10-16"
related:
  - "demo/示例/api/示例-OpenAPI（v1）"
---

# 示例接口文档

## 基本信息

| 项 | 说明 |
|---|---|
| 请求前缀 | `/demo` |

## 调用示例与变量

```bash
BASE='http://127.0.0.1:8080'
TOKEN='x'
```

## 数据权限与口径

| 入参 | 语义 |
|---|---|
| 不传 | 全部 |

## 接口清单

| # | 接口 | 方法 | 权限点 | 说明 |
|---|---|---|---|---|
| 1 | `/demo/list` | GET | `demo:list` | 列表 |

## 1. 列表接口
`GET /demo/list`

### 请求参数（query）
| 字段 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| deptId | Long | 否 | 部门 | 1 |

```bash
curl -s -G "$BASE/demo/list" -H "Authorization: Bearer $TOKEN" --data-urlencode 'deptId=1'
```

### 响应示例
```json
{ "code": 200, "msg": "查询成功", "data": {} }
```

失败响应：`{ "code": 500, "msg": "请至少选择一个商品" }`

## 枚举与字典

| 枚举 | 值 | 含义 | 来源 |
|---|---|---|---|
| status | 1 | 启用 | 字典 |

## 错误信息总表

| 错误信息 | 触发场景 |
|---|---|
| 请至少选择一个商品 | 入参为空 |

## 口径与边界

1. 本轮不做超卖控制。

## 部署前置

1. 先执行迁移 SQL。

## 代码位置

- Controller：`/demo/DemoController.java`

## 待确认

- 无。
"""

NONCONFORMING = """---
title: "验收整改接口文档"
project: "plat_jk"
topic: "验收整改"
type: "api"
status: "pending"
date: "2026-10-14"
related:
  - "plat_jk/监督管理/验收整改模块分析"
---

# 验收整改接口文档

## 一、模块概述

承接整改批次。

## 二、通用说明

### 2.1 认证

RuoYi Token。

## 三、接口明细

### 3.1 list — 列表

请求参数若干。
"""


class LintTests(unittest.TestCase):
    def test_conforming_doc_passes(self):
        fmt = server.lint_doc("api", CONFORMING)
        self.assertTrue(fmt["ok"], f"应通过，missing={fmt['missing']} placeholders={fmt['placeholders']}")

    def test_nonconforming_doc_reports_missing_sections(self):
        fmt = server.lint_doc("api", NONCONFORMING)
        self.assertFalse(fmt["ok"])
        self.assertIn("## 基本信息", fmt["missing"])
        self.assertIn("## 接口清单", fmt["missing"])
        self.assertIn("## 调用示例与变量", fmt["missing"])
        self.assertIn("curl 可执行示例", fmt["missing"])
        self.assertIn("related 关联配套 OpenAPI", fmt["missing"])

    def test_placeholder_is_reported(self):
        text = NONCONFORMING.replace("## 一、模块概述", "## 基本信息\n\n| 项 | 说明 |\n|---|---|\n| 请求前缀 | （待填写） |")
        fmt = server.lint_doc("api", text)
        self.assertIn("基本信息", fmt["placeholders"])

    def test_openapi_without_yaml_block(self):
        fmt = server.lint_doc("openapi", "# 规格\n\n随便写点。")
        self.assertIn("```yaml 代码块", fmt["missing"])
        self.assertIn("openapi 3.x 版本声明", fmt["missing"])


class ScaffoldTests(unittest.TestCase):
    def test_scaffold_inserts_missing_sections(self):
        out, inserted = server.scaffold_api_md(NONCONFORMING)
        self.assertTrue(inserted)
        for key in server.API_MD_ORDER:
            self.assertIn(f"## {key}", out, f"补齐后应含 ## {key}")
        # 脚手架只能补 H2 小节；「逐接口 ### 请求参数/请求体」依赖真实接口，必须人写
        remaining = server.lint_doc("api", out)["missing"]
        self.assertEqual(remaining, ["逐接口小节（### 请求参数/请求体）"])

    def test_scaffold_is_idempotent(self):
        once, _ = server.scaffold_api_md(NONCONFORMING)
        twice, inserted = server.scaffold_api_md(once)
        self.assertEqual(inserted, [])
        self.assertEqual(once, twice)

    def test_scaffold_keeps_existing_sections_in_order(self):
        out, inserted = server.scaffold_api_md(CONFORMING)
        self.assertEqual(inserted, [])  # 已合规：不动
        self.assertEqual(out, CONFORMING)

    def test_scaffold_openapi_adds_yaml(self):
        out, inserted = server.scaffold_openapi_md("# 规格\n\n说明文字。")
        self.assertTrue(inserted)
        self.assertIn("```yaml", out)
        self.assertIn("openapi: 3.0.1", out)
        again, inserted2 = server.scaffold_openapi_md(out)
        self.assertEqual(inserted2, [])
        self.assertEqual(again, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
