"""验证真实上传任务、SQLite 持久化和结果下载接口。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import (
    CreateJobRequest,
    JobStatus,
    LocalizationJob,
    TranslationPolicy,
)
from app.storage import JobRepository
from app.version import LEGACY_PIPELINE_VERSION, PDF_PIPELINE_VERSION


class IdleProcessor:
    """测试用处理器，仅记录任务，不访问外部服务。"""

    def __init__(self, repository: JobRepository) -> None:
        self.repository = repository
        self.submitted_job_ids: list[str] = []

    def submit(self, job_id: str) -> None:
        self.submitted_job_ids.append(job_id)

    def cancel(self, job_id: str) -> LocalizationJob:
        return self.repository.cancel(job_id)

    def shutdown(self) -> None:
        return None


class JobApiTest(unittest.TestCase):
    """任务列表必须完全来自后端持久化数据。"""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_directory = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_empty_database_has_no_demo_jobs(self) -> None:
        """新数据目录必须返回空任务列表，不得注入演示任务。"""

        app = create_app(self.data_directory, IdleProcessor)
        with TestClient(app) as client:
            response = client.get("/api/v1/jobs")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_upload_is_persisted_across_app_restart(self) -> None:
        """上传任务应落盘，并在应用重启后自动重新加入执行队列。"""

        first_app = create_app(self.data_directory, IdleProcessor)
        with TestClient(first_app) as client:
            created = self._upload(client)
            self.assertEqual(created["file_name"], "real-input.pdf")
            self.assertEqual(created["file_size"], len(self._pdf_bytes()))
            self.assertEqual(created["status"], "queued")
            self.assertTrue(
                (self.data_directory / "uploads" / f"{created['id']}.pdf").is_file()
            )

        restarted_app = create_app(self.data_directory, IdleProcessor)
        with TestClient(restarted_app) as client:
            jobs = client.get("/api/v1/jobs").json()
            restarted_processor: IdleProcessor = restarted_app.state.processor

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], created["id"])
        self.assertEqual(jobs[0]["file_name"], "real-input.pdf")
        self.assertEqual(jobs[0]["status"], "queued")
        self.assertIsNone(jobs[0]["error_message"])
        self.assertIn(created["id"], restarted_processor.submitted_job_ids)
        details = restarted_app.state.repository.get_details(created["id"])
        self.assertEqual(details.logs[-1].stage, "recovery")
        self.assertIn("无需重新上传", details.logs[-1].message)

    def test_legacy_restart_failure_is_requeued(self) -> None:
        """旧版标记为服务重启失败的任务也应复用源文件自动恢复。"""

        repository = JobRepository(self.data_directory)
        repository.initialize()
        source_path = repository.upload_directory / "legacy-job.pdf"
        source_path.write_bytes(self._pdf_bytes())
        repository.create(
            "legacy-job",
            CreateJobRequest(
                file_name="legacy-job.pdf",
                file_size=source_path.stat().st_size,
                model="provider-model-v1",
                translation_policy=TranslationPolicy(),
            ),
            source_path,
        )
        repository.update_status(
            "legacy-job",
            JobStatus.FAILED,
            15,
            error_message="服务重启导致任务中断，请重新上传文档",
        )

        restarted_app = create_app(self.data_directory, IdleProcessor)
        with TestClient(restarted_app) as client:
            recovered_job = client.get("/api/v1/jobs/legacy-job").json()
            restarted_processor: IdleProcessor = restarted_app.state.processor

        self.assertEqual(recovered_job["status"], "queued")
        self.assertEqual(recovered_job["progress"], 0)
        self.assertIsNone(recovered_job["error_message"])
        self.assertIn("legacy-job", restarted_processor.submitted_job_ids)

    def test_download_returns_only_existing_completed_pdf(self) -> None:
        """只有真实结果文件存在且任务完成时才能下载。"""

        app = create_app(self.data_directory, IdleProcessor)
        with TestClient(app) as client:
            created = self._upload(client)
            unavailable = client.get(f"/api/v1/jobs/{created['id']}/download")
            self.assertEqual(unavailable.status_code, 409)

            repository: JobRepository = app.state.repository
            result_path = repository.result_directory / f"{created['id']}.pdf"
            result_bytes = b"%PDF-1.4\nDocWeave real result\n%%EOF\n"
            result_path.write_bytes(result_bytes)
            repository.update_status(
                created["id"],
                JobStatus.COMPLETED,
                100,
                result_path=result_path,
                result_file_name="real-input_中文版.pdf",
            )

            response = client.get(f"/api/v1/jobs/{created['id']}/download")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.content, result_bytes)
        self.assertIn("filename*", response.headers["content-disposition"])
        self.assertEqual(response.headers["x-docweave-quality-status"], "passed")

    def test_job_details_include_persisted_log_and_metrics(self) -> None:
        """任务详情接口必须返回真实日志和资源指标。"""

        app = create_app(self.data_directory, IdleProcessor)
        with TestClient(app) as client:
            created = self._upload(client)
            response = client.get(f"/api/v1/jobs/{created['id']}/details")

        self.assertEqual(response.status_code, 200)
        details = response.json()
        self.assertEqual(details["job"]["id"], created["id"])
        self.assertEqual(details["logs"][0]["stage"], "queued")
        self.assertIn("等待并发调度", details["logs"][0]["message"])
        self.assertEqual(details["metrics"]["duration_ms"], 0)
        self.assertFalse(details["metrics"]["token_usage_available"])
        self.assertEqual(details["metrics"]["translation_memory_hit_count"], 0)
        self.assertEqual(details["quality_issues"], [])

    def test_glossary_is_server_persisted_and_snapshotted_per_job(self) -> None:
        """术语必须存入后端，并在任务创建时冻结快照。"""

        app = create_app(self.data_directory, IdleProcessor)
        with TestClient(app) as client:
            created_term = client.post(
                "/api/v1/glossary",
                json={
                    "source_text": "Coverlay",
                    "target_text": "覆盖膜",
                    "category": "材料",
                },
            )
            self.assertEqual(created_term.status_code, 201)
            created = self._upload(client)
            terms = client.get("/api/v1/glossary").json()

        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0]["target_text"], "覆盖膜")
        snapshot = app.state.repository.get_glossary_snapshot(created["id"])
        self.assertEqual(snapshot[0]["source_text"], "Coverlay")
        self.assertEqual(snapshot[0]["target_text"], "覆盖膜")

    def test_needs_review_result_is_downloaded_as_customer_result(self) -> None:
        """待复核结果应按正常文件名交付并保留质量响应头。"""

        app = create_app(self.data_directory, IdleProcessor)
        with TestClient(app) as client:
            created = self._upload(client)
            repository: JobRepository = app.state.repository
            result_path = repository.result_directory / f"{created['id']}.pdf"
            result_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
            repository.update_status(
                created["id"],
                JobStatus.NEEDS_REVIEW,
                99,
                result_path=result_path,
                result_file_name="draft_cn.pdf",
            )

            job = client.get(f"/api/v1/jobs/{created['id']}").json()
            response = client.get(f"/api/v1/jobs/{created['id']}/download")

        self.assertTrue(job["result_available"])
        self.assertTrue(job["result_downloadable"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["x-docweave-quality-status"],
            "review-recommended",
        )
        self.assertIn("draft_cn.pdf", response.headers["content-disposition"])
        self.assertNotIn(
            "%E8%B4%A8%E6%A3%80%E6%9C%AA%E9%80%9A%E8%BF%87",
            response.headers["content-disposition"],
        )

    def test_legacy_result_is_hidden_and_can_be_reprocessed(self) -> None:
        """旧版大文本框产物必须隔离，并可复用原文件按新版重新排队。"""

        app = create_app(self.data_directory, IdleProcessor)
        with TestClient(app) as client:
            created = self._upload(client)
            repository: JobRepository = app.state.repository
            result_path = repository.result_directory / f"{created['id']}.pdf"
            result_path.write_bytes(b"%PDF-1.4\nlegacy result\n%%EOF\n")
            repository.update_status(
                created["id"],
                JobStatus.COMPLETED,
                100,
                result_path=result_path,
                result_file_name="legacy_cn.pdf",
            )
            with sqlite3.connect(repository.database_path) as connection:
                connection.execute(
                    """
                    UPDATE localization_jobs
                    SET pipeline_version = ?
                    WHERE id = ?
                    """,
                    (LEGACY_PIPELINE_VERSION, created["id"]),
                )

            legacy_job = client.get(f"/api/v1/jobs/{created['id']}").json()
            unavailable = client.get(f"/api/v1/jobs/{created['id']}/download")
            reprocessed = client.post(
                f"/api/v1/jobs/{created['id']}/reprocess"
            )
            processor: IdleProcessor = app.state.processor

        self.assertTrue(legacy_job["result_outdated"])
        self.assertFalse(legacy_job["result_available"])
        self.assertFalse(legacy_job["result_downloadable"])
        self.assertEqual(unavailable.status_code, 409)
        self.assertEqual(reprocessed.status_code, 202)
        self.assertEqual(reprocessed.json()["status"], "queued")
        self.assertEqual(
            reprocessed.json()["pipeline_version"],
            PDF_PIPELINE_VERSION,
        )
        self.assertFalse(reprocessed.json()["result_outdated"])
        self.assertIn(created["id"], processor.submitted_job_ids)
        self.assertFalse(result_path.exists())
        self.assertEqual(
            len(list(repository.legacy_result_directory.glob("*.pdf"))),
            1,
        )

    def _upload(self, client: TestClient) -> dict[str, object]:
        response = client.post(
            "/api/v1/jobs",
            data={
                "model": "provider-model-v1",
                "strategy": "智能路由",
                "translation_policy": (
                    '{"source_language":"auto","target_language":"zh-CN",'
                    '"glossary_learning_mode":"review"}'
                ),
            },
            files={"file": ("real-input.pdf", self._pdf_bytes(), "application/pdf")},
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    @staticmethod
    def _pdf_bytes() -> bytes:
        return b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


if __name__ == "__main__":
    unittest.main()
