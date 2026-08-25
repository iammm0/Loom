import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SKILL_SCRIPT = (
    Path(__file__).parent.parent.parent / "docs" / "skill" / "loom_agent.py"
)
SKILL_DOCUMENT = SKILL_SCRIPT.with_name("SKILL.md")
SPEC = importlib.util.spec_from_file_location("loom_agent_skill", SKILL_SCRIPT)
loom_agent = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(loom_agent)


MINIMAL_CONFIG = """\
llm_provider = "moonshot"
moonshot_api_key = ""
deepseek_api_key = ""
volcengine_api_key = ""
oneapi_api_key = ""
oneapi_base_url = ""
oneapi_model_name = ""

[seedance]
api_key = ""
"""


class TestLoomAgentSkill(unittest.TestCase):
    def create_project(self, root: Path) -> None:
        """创建足够完成安装和配置检查的最小项目结构。"""
        root.mkdir()
        (root / "cli.py").write_text("", encoding="utf-8")
        (root / "config.example.toml").write_text(
            MINIMAL_CONFIG, encoding="utf-8"
        )
        (root / "pyproject.toml").write_text(
            'name = "video-loom"\n', encoding="utf-8"
        )

    def test_skill_runs_helper_from_its_working_directory(self):
        """确保 Windows Agent 不会在命令中嵌入易被破坏的绝对路径。"""
        text = SKILL_DOCUMENT.read_text(encoding="utf-8")

        self.assertIn(
            "uv run --no-project --python 3.11 python loom_agent.py --subject",
            text,
        )
        self.assertIn("workdir=SKILL_DIR", text)
        self.assertNotIn('python "<SKILL_DIR>/loom_agent.py"', text)
        self.assertNotIn("MoneyPrinterTurbo", text)
        self.assertNotIn("mpt_agent.py", text)

    def test_first_run_only_requests_missing_api_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "video-loom"
            self.create_project(root)
            output = io.StringIO()

            with patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
                code = loom_agent.main(
                    ["--subject", "人工智能如何改变生活", "--root", str(root)]
                )

            self.assertEqual(code, loom_agent.NEEDS_INPUT_EXIT_CODE)
            text = output.getvalue()
            self.assertIn("LOOM_NEEDS_INPUT", text)
            self.assertIn("MISSING=moonshot_api_key", text)
            self.assertIn("MISSING=seedance.api_key", text)
            self.assertIn("LLM_PROVIDER_OPTION=deepseek|DeepSeek|", text)
            self.assertIn(
                "LLM_PROVIDER_OPTION=oneapi|Other OpenAI-compatible provider|",
                text,
            )
            self.assertNotIn("Alibaba Cloud Qwen", text)
            self.assertNotIn("Microsoft Azure OpenAI", text)
            self.assertNotIn("xAI Grok", text)
            self.assertIn(
                f"SEEDANCE_API_KEY_URL={loom_agent.SEEDANCE_API_KEY_URL}", text
            )
            self.assertNotIn("PEXELS", text)

    def test_environment_keys_are_written_without_being_logged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(MINIMAL_CONFIG, encoding="utf-8")
            output = io.StringIO()
            llm_key = "secret-llm-key"
            seedance_key = "secret-seedance-key"

            with patch.dict(
                os.environ,
                {
                    "LOOM_LLM_PROVIDER": "deepseek",
                    "LOOM_LLM_API_KEY": llm_key,
                    "LOOM_SEEDANCE_API_KEY": seedance_key,
                },
                clear=True,
            ), redirect_stdout(output):
                loom_agent.apply_environment_config(config_path)

            config = config_path.read_text(encoding="utf-8")
            self.assertIn('llm_provider = "deepseek"', config)
            self.assertIn(f'deepseek_api_key = "{llm_key}"', config)
            self.assertIn(f'api_key = "{seedance_key}"', config)
            self.assertNotIn(llm_key, output.getvalue())
            self.assertNotIn(seedance_key, output.getvalue())

    def test_existing_provider_key_is_reused_without_asking_user(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            secret = "already-configured-deepseek-key"
            config_path.write_text(
                MINIMAL_CONFIG.replace(
                    'deepseek_api_key = ""', f'deepseek_api_key = "{secret}"'
                ).replace(
                    '[seedance]\napi_key = ""',
                    '[seedance]\napi_key = "seedance-key"',
                ),
                encoding="utf-8",
            )
            output = io.StringIO()

            with redirect_stdout(output):
                provider = loom_agent.reuse_existing_llm_provider(config_path)
            _, missing = loom_agent.missing_config(config_path, [])

            self.assertEqual(provider, "deepseek")
            self.assertEqual(missing, [])
            self.assertIn(
                'llm_provider = "deepseek"',
                config_path.read_text(encoding="utf-8"),
            )
            self.assertNotIn(secret, output.getvalue())

    def test_volcengine_key_satisfies_seedance_without_duplicate_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                MINIMAL_CONFIG.replace(
                    'moonshot_api_key = ""', 'moonshot_api_key = "configured"'
                ).replace(
                    'volcengine_api_key = ""',
                    'volcengine_api_key = "shared-ark-key"',
                ),
                encoding="utf-8",
            )

            _, missing = loom_agent.missing_config(config_path, [])
            self.assertEqual(missing, [])

    def test_only_missing_seedance_key_does_not_ask_for_llm_again(self):
        output = io.StringIO()

        with redirect_stdout(output):
            code = loom_agent.report_missing_config(
                "deepseek", ["seedance.api_key"]
            )

        text = output.getvalue()
        self.assertEqual(code, loom_agent.NEEDS_INPUT_EXIT_CODE)
        self.assertIn(
            f"SEEDANCE_API_KEY_URL={loom_agent.SEEDANCE_API_KEY_URL}", text
        )
        self.assertNotIn("LLM_PROVIDER_OPTIONS_BEGIN", text)

    def test_custom_openai_compatible_provider_requires_connection_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                MINIMAL_CONFIG.replace(
                    'llm_provider = "moonshot"', 'llm_provider = "oneapi"'
                ).replace('oneapi_api_key = ""', 'oneapi_api_key = "key"'),
                encoding="utf-8",
            )

            provider, missing = loom_agent.missing_config(config_path, [])
            output = io.StringIO()
            with redirect_stdout(output):
                loom_agent.report_missing_config(provider, missing)

            self.assertEqual(provider, "oneapi")
            self.assertEqual(
                missing,
                ["oneapi_base_url", "oneapi_model_name", "seedance.api_key"],
            )
            self.assertIn("OPENAI_COMPATIBLE_REQUIRED=", output.getvalue())

    def test_custom_openai_compatible_environment_is_mapped_to_oneapi(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(MINIMAL_CONFIG, encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "LOOM_LLM_PROVIDER": "openai_compatible",
                    "LOOM_LLM_API_KEY": "custom-key",
                    "LOOM_LLM_BASE_URL": "https://llm.example.com/v1",
                    "LOOM_LLM_MODEL_NAME": "example-model",
                },
                clear=True,
            ):
                loom_agent.apply_environment_config(config_path)

            config = config_path.read_text(encoding="utf-8")
            self.assertIn('llm_provider = "oneapi"', config)
            self.assertIn('oneapi_api_key = "custom-key"', config)
            self.assertIn(
                'oneapi_base_url = "https://llm.example.com/v1"', config
            )
            self.assertIn('oneapi_model_name = "example-model"', config)

    def test_zip_extraction_rejects_parent_directory_escape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "unsafe.zip"
            destination = Path(temp_dir) / "extract"
            destination.mkdir()
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "unsafe")

            with zipfile.ZipFile(archive_path) as archive, self.assertRaises(
                loom_agent.SkillError
            ):
                loom_agent._safe_extract(archive, destination)

    def test_generation_returns_only_non_empty_final_video(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_id = "12345678-1234-1234-1234-123456789abc"

            def finish_cli(command, **kwargs):
                task_dir = root / "storage" / "tasks" / task_id
                task_dir.mkdir(parents=True)
                (task_dir / "final-1.mp4").write_bytes(b"video")
                return SimpleNamespace(returncode=0)

            with (
                patch.object(loom_agent.shutil, "which", return_value="uv"),
                patch.object(loom_agent, "run_checked"),
                patch.object(loom_agent.uuid, "uuid4", return_value=task_id),
                patch.object(
                    loom_agent.subprocess, "run", side_effect=finish_cli
                ) as run_mock,
            ):
                videos, task_dir, log_path, result_path = loom_agent.generate_video(
                    root,
                    "测试主题",
                    ["--video-aspect", "16:9", "--stop-at", "script"],
                )

            self.assertEqual(videos, [(task_dir / "final-1.mp4").resolve()])
            self.assertTrue(log_path.name.startswith("run-"))
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["video_files"], [str(videos[0])])
            command = run_mock.call_args.args[0]
            voice_index = command.index("--voice-name")
            self.assertEqual(
                command[voice_index + 1], loom_agent.DEFAULT_VOICE_NAME
            )
            self.assertEqual(command[-2:], ["--stop-at", "video"])

    def test_generation_failure_prints_original_model_error(self):
        """生成失败时保留模型原始错误，避免 Skill 层猜测供应商语义。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_id = "12345678-1234-1234-1234-123456789abc"
            model_error = "provider error: model is unavailable for this account"
            stderr = io.StringIO()

            def reject_model(command, **kwargs):
                kwargs["stdout"].write(model_error + "\n")
                return SimpleNamespace(returncode=1)

            with (
                patch.object(loom_agent.shutil, "which", return_value="uv"),
                patch.object(loom_agent, "run_checked"),
                patch.object(loom_agent.uuid, "uuid4", return_value=task_id),
                patch.object(
                    loom_agent.subprocess, "run", side_effect=reject_model
                ),
                redirect_stderr(stderr),
                self.assertRaises(loom_agent.SkillError),
            ):
                loom_agent.generate_video(root, "测试主题", [])

            self.assertIn(model_error, stderr.getvalue())
            result = json.loads(
                loom_agent.result_manifest_path(root).read_text(encoding="utf-8")
            )
            self.assertEqual(result["status"], "failed")

    def test_successful_dependency_sync_does_not_print_package_list(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        result = SimpleNamespace(
            returncode=0,
            stdout="Installed package-a\nInstalled package-b\n",
        )

        with patch.object(
            loom_agent.subprocess, "run", return_value=result
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            loom_agent.run_checked(["uv", "sync", "--frozen"], cwd=Path.cwd())

        self.assertNotIn("package-a", stdout.getvalue())
        self.assertNotIn("package-a", stderr.getvalue())

    def test_explicit_voice_is_not_overridden(self):
        self.assertTrue(
            loom_agent.has_cli_option(
                ["--voice-name", "en-US-JennyNeural-Female"], "--voice-name"
            )
        )
        self.assertTrue(
            loom_agent.has_cli_option(
                ["--voice-name=en-US-JennyNeural-Female"], "--voice-name"
            )
        )


if __name__ == "__main__":
    unittest.main()
