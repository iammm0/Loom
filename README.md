# AutoEditing

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](pyproject.toml)
[![API](https://img.shields.io/badge/API-FastAPI-009688.svg)](main.py)
[![WebUI](https://img.shields.io/badge/WebUI-TanStack-5c6ac4.svg)](webui/src/main.tsx)

AutoEditing 是一个视频剪辑 Agent 平台。它围绕主题生成脚本文案、按分镜准备素材、合成配音与字幕、接入剪辑工具并导出成片。主链路由 LangGraph 编排，缺素材时自动走素材库、在线检索和 Seedance，不再等待人工上传确认。

本仓库基于 [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) 继续二次开发。

## 核心能力

- AI 脚本生成：根据主题、语言、段落数量和附加要求生成短视频文案。
- AI 导演规划：推断段落结构、配音速度、镜头节奏、转场和配乐参数。
- 素材自动补齐：先匹配素材库和在线来源，缺镜时自动调用 Seedance；补不齐则任务失败。
- 配音与字幕：TTS 配音，以及 Edge/Whisper 字幕链路。
- 剪辑 tool 层：探测、裁剪、拼接、缩放、变速、字幕、混音、静音粗剪、时间轴导出。
- 剪辑任务：列表、详情、批量操作、取消、重试、删除、预览和下载。
- 素材管理：上传、检索、打标、标签管理和分镜自动入库。
- FastAPI 接口与 TanStack WebUI 工作台。

## 技术栈

- Python 3.11+
- FastAPI / Uvicorn
- LangGraph
- Vite / React / TypeScript
- TanStack Router、Query、Table、Form
- MoviePy / FFmpeg
- faster-whisper / Edge TTS
- LiteLLM / OpenAI-compatible API
- Redis，可选
- Docker / Docker Compose

## 系统要求

- Python 3.11 或更高版本。
- [uv](https://github.com/astral-sh/uv)，推荐用于同步依赖。
- FFmpeg。
- Node.js 18+，用于构建 WebUI。
- 可用的大模型、TTS；缺失分镜在配置 Seedance 后会自动生成。

## 快速开始

```bash
cp config.example.toml config.toml
uv sync --frozen
./webui.sh
```

Windows：

```bat
webui.bat
```

工作台默认地址：

```text
http://127.0.0.1:8080
```

API 文档：

```text
http://127.0.0.1:8080/docs
```

开发时也可以分开启动：

```bash
uv run python main.py
cd webui && npm install && npm run dev
```

前端开发地址为 `http://127.0.0.1:8501`，并代理到 API `8080`。

## Docker 运行

```bash
docker compose up --build
```

| 服务 | 地址 |
| --- | --- |
| WebUI | `http://127.0.0.1:8501` |
| API | `http://127.0.0.1:8080` |
| API 文档 | `http://127.0.0.1:8080/docs` |

## 配置说明

```bash
cp config.example.toml config.toml
```

不要提交本地 `config.toml`。常用项包括 `llm_provider`、各服务 API Key、`material_strategy`、`rough_cut_enabled`。

选择 `ai_generated` 时，Agent 会规划分镜并自动生成缺失镜头，不再停在上传关卡。选择 `local_first` 时先匹配素材库和在线素材，仍缺镜则自动生成。

## 基本使用流程

1. 复制配置并填写 LLM、TTS、Seedance 等密钥。
2. 启动工作台，打开自动剪辑页。
3. 输入主题或文案，创建任务。
4. Agent 自动完成文案、配音、分镜、剪辑和导出。
5. 在剪辑任务中预览、下载、重试或删除。

## API 概览

API 统一挂载在 `/api/v1` 下。完整参数以 `/docs` 为准。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/scripts` | 生成视频脚本文案 |
| `POST` | `/api/v1/videos` | 创建完整视频生成任务 |
| `GET` | `/api/v1/tasks` | 查询任务列表 |
| `GET` | `/api/v1/tasks/{task_id}` | 查询单个任务详情 |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | 取消任务 |
| `POST` | `/api/v1/tasks/{task_id}/retry` | 重试失败任务 |
| `GET` | `/api/v1/materials` | 查询素材库 |
| `GET` | `/api/v1/settings` | 读取工作台配置 |
| `PUT` | `/api/v1/settings` | 保存工作台配置 |
| `GET` | `/api/v1/billing/seedance` | 查询 Seedance 账单 |

创建视频任务仍走 `/api/v1/videos`，内部入队 LangGraph Agent。

## 项目结构

```text
app/agent            LangGraph 编排
app/tools            生产与剪辑 tool 层
app/timeline         时间轴模型
app/services         文案、TTS、素材、合成等现有能力
webui/               TanStack WebUI
resource/            字体、公共资源和内置音乐
storage/             本地缓存、素材和任务产物
test/                测试
config.example.toml  配置模板
main.py              API 与 WebUI 托管入口
```

## 开发与测试

```bash
uv sync --frozen
uv run python -X utf8 -m pytest -q test
uv run ruff check .
```

前端：

```bash
cd webui
npm install
npm run build
```

## 数据与安全

- `config.toml` 可能包含密钥，只保留在本地或部署环境中。
- `storage/` 会保存上传素材和生成结果。
- 对外暴露 API 前请配置鉴权、HTTPS 和访问控制。

## 许可

本仓库沿用 [MIT License](LICENSE)。使用时请同时遵守上游项目以及第三方依赖、模型服务、素材服务的许可和使用条款。
