from django.test import SimpleTestCase, TestCase
from apps.llm.models import ModelConfig


class VertexProviderEnumTests(SimpleTestCase):
    def test_enum_value_present(self):
        assert ModelConfig.Provider.VERTEX_MODEL_GARDEN == "vertex_model_garden"
        assert "vertex_model_garden" in ModelConfig.Provider.values


class VertexResolveRuntimeTests(TestCase):
    def test_resolve_runtime_builds_in_memory_config(self):
        cfg = ModelConfig.resolve_runtime(
            "vertex_model_garden", "deepseek-ai/deepseek-v3.2-maas"
        )
        assert cfg is not None
        assert cfg.provider == "vertex_model_garden"
        assert cfg.model_name == "deepseek-ai/deepseek-v3.2-maas"
        assert cfg.api_key_env_var == "GOOGLE_CLOUD_PROJECT"
        assert cfg.pk is None  # never persisted
