# video-loom 测试目录

本目录包含 **video-loom** 的单元测试与控制器测试。

## 目录结构

- `services/`：按领域划分的单元测试与控制器测试
  - `test_task.py`：任务流水线
  - `test_task_manager.py`：内存与 Redis 队列
  - `test_controller_*.py`：按控制器拆分的 API 测试
  - `test_video.py`、`test_voice.py`：媒体服务
  - `test_loom_agent_skill.py`：`docs/skill` 成片 Skill
- `test_main.py`：应用入口测试

## 运行测试

CI 使用 pytest，会同时收集现有的 `unittest.TestCase`：

```bash
# 运行全部测试
uv run python -X utf8 -m pytest -q test

# 运行指定文件
uv run python -X utf8 -m pytest -q test/services/test_video.py

# 运行指定测试类
uv run python -X utf8 -m pytest -q test/services/test_video.py::TestVideoService

# 运行指定方法
uv run python -X utf8 -m pytest -q test/services/test_video.py::TestVideoService::test_preprocess_video
```

与 CI 相同的分支覆盖率检查：

```bash
uv run python -X utf8 -m coverage run -m pytest -q test
uv run python -m coverage report
```

对接真实 TTS / LLM 的测试默认跳过。需要跑这些用例时，设置 `MPT_RUN_INTEGRATION_TESTS=1` 并提供对应密钥。

## 新增测试

1. 文件命名为 `test_<domain>.py`，每个文件聚焦一个领域。
2. 控制器测试按文件拆分，例如 `test_controller_video.py`。
3. 可用 pytest 函数或 `unittest.TestCase`；pytest 会收集两者。
4. 测试函数与方法使用 `test_` 前缀。

## 测试资源

测试所需资源文件放在 `test/resources`。
