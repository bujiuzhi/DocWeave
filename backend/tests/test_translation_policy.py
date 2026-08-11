"""验证全文翻译和受控保留规则的模型指令。"""

import unittest

from app.main import CreateJobRequest, GlossaryLearningMode, LanguageCode, TranslationPolicy
from app.services.translation_policy import build_translation_instruction


class TranslationPolicyTest(unittest.TestCase):
    """翻译指令应明确覆盖全部自然语言，同时保留受控内容。"""

    def test_default_policy_translates_all_languages_to_chinese(self) -> None:
        """默认策略包含全文中文化要求。"""

        instruction = build_translation_instruction(TranslationPolicy())

        self.assertIn("自动识别的所有源语言", instruction)
        self.assertIn("输出语言：简体中文", instruction)
        self.assertIn("所有可翻译的自然语言内容", instruction)
        self.assertIn("图表标题、图例、图表标签", instruction)
        self.assertIn("术语库命中项", instruction)

    def test_policy_keeps_task_protected_terms(self) -> None:
        """自定义保留词应写入模型指令。"""

        policy = TranslationPolicy(
            source_language=LanguageCode.JA,
            target_language=LanguageCode.ZH_CN,
            protected_terms=["FCCL", "Coverlay"],
        )
        instruction = build_translation_instruction(policy)

        self.assertIn("本任务额外保留词：FCCL、Coverlay", instruction)

    def test_job_keeps_task_model_and_glossary_learning_mode(self) -> None:
        """模型和术语学习策略应作为任务级配置保存。"""

        request = CreateJobRequest(
            file_name="example.pdf",
            file_size=128,
            model="provider-model-v1",
            translation_policy=TranslationPolicy(
                glossary_learning_mode=GlossaryLearningMode.AUTO,
            ),
        )

        self.assertEqual(request.model, "provider-model-v1")
        self.assertEqual(request.translation_policy.glossary_learning_mode, GlossaryLearningMode.AUTO)

    def test_glossary_snapshot_and_company_suffix_rules_enter_instruction(self) -> None:
        """正式术语译文与完整公司名保留规则必须进入模型指令。"""

        instruction = build_translation_instruction(
            TranslationPolicy(),
            [
                {
                    "source_text": "Coverlay",
                    "target_text": "覆盖膜",
                    "category": "材料",
                }
            ],
        )

        self.assertIn("Coverlay → 覆盖膜", instruction)
        self.assertIn("Company、Corporation、Inc.、Ltd.", instruction)
        self.assertIn("完整公司、品牌或机构名称应整体保留官方写法", instruction)


if __name__ == "__main__":
    unittest.main()
