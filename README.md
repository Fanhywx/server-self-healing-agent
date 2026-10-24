
# 服务端故障自愈 Agent

一个基于多模型协作的 Agent 系统，通过工单驱动的人机协同工作流，自动检测、诊断并修复服务端应用故障。

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                     用户 / Dashboard                         │
│              (Streamlit Web 控制台)                          │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────┐
│                    Ticket Agent (编排器)                     │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐   │
│  │ Filter  │ │ Generate │ │ Patch    │ │ Sandbox      │   │
│  │ (Qwen3) │ │ (DeepSeek)│ │ (Kimi K2)│ │ Validation   │   │
│  └────┬────┘ └────┬─────┘ └────┬─────┘ └──────┬───────┘   │
│       │           │            │               │             │
│       ▼           ▼            ▼               ▼             │
│  [本地]     [云端 API]  [云端 API]    [本地:8081]            │
│  QWEN3-7B    DeepSeek V4.1  Kimi K2.7      Flask 目标应用     │
│  GPU         Flash         Code           + 视觉校验          │
└─────────────────────────────────────────────────────────────┘
```

## 核心组件

| 组件 | 文件 | 说明 |
|------|------|------|
| **目标应用** | `target_app.py` | Flask Web 应用，包含故意设置的故障用于测试 |
| **控制台** | `dashboard.py` | Streamlit 管理控制台，用于监控、工单管理和人工审核 |
| **自愈引擎** | `self_heal.py` | 编排完整的修复流水线 |
| **诊断模块** | `diagnose.py` | 错误分析与根因定位 |
| **工单 Agent** | `ticket_agent.py` | 多模型协作编排引擎 |
| **离线回退** | `offline_fallback.py` | 云 API 不可用时的本地修复方案 |
| **通用工具** | `common_utils.py` | 共享工具函数（日志、配置加载等） |
| **视觉校验** | `visual_check.py` | 基于截图的 UI 验证模块 |

## 技术栈

- **后端**: Python 3.10+, Flask, Streamlit
- **AI 模型**:
  - DeepSeek V4.1 Flash — 故障诊断与解决方案生成
  - Kimi K2.7 Code — 代码补丁生成
  - DeepSeek V4 Flash Vision — 截图视觉验证
  - QWEN3-7B（本地）— 初始请求过滤与离线回退
- **API 提供商**: ModelVerse (`https://api.modelverse.cn/v1`)
- **部署**: Ubuntu 22.04, Nginx 反向代理

## 快速开始

### 前置条件

```bash
pip install flask streamlit requests openai pillow pyyaml
```

### 配置

编辑 `config.json`:

```json
{
  "modelverse_api_key": "YOUR_API_KEY_HERE",
  "target_app_port": 8080,
  "dashboard_port": 8501,
  "sandbox_port": 8081,
  "preview_port": 8095
}
```

> ⚠️ 部署前请将 `YOUR_API_KEY_HERE` 替换为实际的 ModelVerse API Key。
> ⚠️ 模型使用的api为[⚠️](https://api.modelverse.cn/v1/chat/completions)
### 运行系统

```bash
# 终端 1：启动目标应用
python target_app.py

# 终端 2：启动控制台
streamlit run dashboard.py
```

访问 `http://localhost:8501` 打开控制台。

## 工作流程

自愈过程遵循结构化的工单驱动工作流：

1. **请求过滤** — 本地 QWEN3-7B 模型过滤传入的故障报告，判断是否需要自动化处理。

2. **故障诊断** — DeepSeek V4.1 Flash 分析错误日志和堆栈跟踪，识别根因。

3. **方案生成** — DeepSeek 根据诊断结果生成修复策略。

4. **补丁生成** — Kimi K2.7 Code 生成实际修复代码补丁。

5. **沙箱验证** — 补丁代码部署到隔离的沙箱环境（端口 8081）进行功能测试。

6. **视觉验证** — deepseek-vision-exp 截取补丁后应用的截图，验证 UI 正确性。

7. **人工审核** — 所有补丁进入 `await_review` 状态，需用户手动审批后方可合并。

8. **预览与合并** — 审批通过的补丁在端口 8095 预览，随后由用户在控制台中合并到生产环境。

## 功能特性

- **多模型协作**：利用不同模型的专业能力完成修复流程各阶段
- **工单驱动**：结构化流程，状态流转清晰，可追溯审计
- **沙箱隔离**：补丁先在隔离环境中验证，不影响生产环境
- **视觉验证**：AI 驱动的截图对比确保 UI 变更正确
- **人机协同**：最终合并需用户显式批准
- **离线回退**：本地 QWEN3-7B 可处理简单故障，无需云端依赖
- **实时监控**：Streamlit 控制台实时展示所有工单和处理状态

## 文件参考

| 文件 | 用途 |
|------|------|
| `config.json` | 配置文件（API Key、端口、模型设置） |
| `main.py` | 服务入口，启动所有组件 |
| `target_app.py` | Flask 目标应用，含故意设置的故障 |
| `dashboard.py` | Streamlit 管理控制台 |
| `self_heal.py` | 主自愈编排逻辑 |
| `diagnose.py` | 故障诊断模块 |
| `ticket_agent.py` | 多模型工单处理 Agent |
| `offline_fallback.py` | 本地回退修复机制 |
| `visual_check.py` | 截图视觉验证模块 |
| `common_utils.py` | 通用工具函数 |

## 设计说明

- 本系统作为使用 LLM Agent 实现服务端故障自愈的概念验证
- 所有云端 API 调用均通过 ModelVerse 统一端点
- 本地模型在有 GPU 时运行；支持 CPU 降级
- 沙箱环境使用独立的 Flask 实例，将测试补丁与主应用隔离
- 视觉检查采用截图捕获加 AI 视觉模型，而非 DOM 检查
*（内容由AI生成，仅供参考）*
