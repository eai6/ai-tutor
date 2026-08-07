"""Material-processing dispatch — ECS RunTask, Azure fallthrough, local fallback.

SimpleTestCase rather than bare pytest functions to match the rest of the
suite; pytest-django is not installed.
"""
from __future__ import annotations

import os
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.dashboard import job_dispatch


class _FakeECS:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    def run_task(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


ECS_ENV = {
    "ECS_CLUSTER": "aitutor-prod",
    "ECS_MATERIAL_TASK_DEFINITION": "aitutor-prod-material:7",
    "ECS_SUBNETS": "subnet-aaa,subnet-bbb",
    "ECS_SECURITY_GROUPS": "sg-tasks",
}

AZURE_ENV = {
    "AZURE_RESOURCE_GROUP": "aitutor-pixel-rg",
    "AZURE_MATERIAL_JOB_NAME": "aitutor-pixel-material-job",
    "AZURE_SUBSCRIPTION_ID": "sub-1234",
}

ALL_KEYS = list(ECS_ENV) + list(AZURE_ENV)

OK_RESPONSE = {
    "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:1234:task/aitutor-prod/abc123"}],
    "failures": [],
}


class _DispatchTestCase(SimpleTestCase):
    """Clears every cloud env var so each test states its own world."""

    def setUp(self):
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ALL_KEYS:
            os.environ.pop(key, None)

    def _set(self, mapping):
        for key, value in mapping.items():
            os.environ[key] = value

    def _fake_ecs(self, response):
        client = _FakeECS(response)
        patcher = patch.object(job_dispatch, "_ecs_client", lambda: client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client


class ECSDispatchTests(_DispatchTestCase):

    def setUp(self):
        super().setUp()
        self._set(ECS_ENV)

    def test_dispatch_returns_the_task_id_from_the_arn(self):
        self._fake_ecs(OK_RESPONSE)

        self.assertEqual(job_dispatch.dispatch_material_job(42, mode="rich"), "abc123")

    def test_dispatch_overrides_only_the_command(self):
        """The task definition already carries image, resources, env and
        secrets — unlike a Container Apps Job, nothing else needs restating."""
        client = self._fake_ecs(OK_RESPONSE)

        job_dispatch.dispatch_material_job(42, mode="fast")

        call = client.calls[0]
        self.assertEqual(call["cluster"], "aitutor-prod")
        self.assertEqual(call["taskDefinition"], "aitutor-prod-material:7")
        self.assertEqual(call["launchType"], "FARGATE")
        overrides = call["overrides"]["containerOverrides"]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(
            overrides[0]["command"],
            ["python", "manage.py", "process_material", "42", "--mode", "fast"],
        )
        self.assertNotIn("image", overrides[0])
        self.assertNotIn("environment", overrides[0])

    def test_dispatch_places_the_task_in_the_configured_private_subnets(self):
        client = self._fake_ecs(OK_RESPONSE)

        job_dispatch.dispatch_material_job(42)

        vpc = client.calls[0]["networkConfiguration"]["awsvpcConfiguration"]
        self.assertEqual(vpc["subnets"], ["subnet-aaa", "subnet-bbb"])
        self.assertEqual(vpc["securityGroups"], ["sg-tasks"])
        self.assertEqual(vpc["assignPublicIp"], "DISABLED")

    def test_a_run_task_failure_raises_rather_than_reporting_success(self):
        self._fake_ecs({"tasks": [], "failures": [{"reason": "RESOURCE:MEMORY"}]})

        with self.assertRaises(RuntimeError) as ctx:
            job_dispatch.dispatch_material_job(42)
        self.assertIn("RESOURCE:MEMORY", str(ctx.exception))

    def test_an_empty_task_list_raises(self):
        self._fake_ecs({"tasks": [], "failures": []})

        with self.assertRaises(RuntimeError):
            job_dispatch.dispatch_material_job(42)

    def test_ecs_wins_when_both_clouds_are_configured(self):
        """Both sets of vars should never coexist, but if they do the AWS
        path must not silently dispatch to Azure."""
        self._set(AZURE_ENV)
        self._fake_ecs(OK_RESPONSE)

        self.assertEqual(job_dispatch.dispatch_material_job(42), "abc123")


class AzurePathStillIntactTests(_DispatchTestCase):
    """Azure dispatches live material jobs. These guard that path from being
    broken in passing while the AWS backend is added."""

    def test_the_azure_backend_is_still_present(self):
        self.assertTrue(hasattr(job_dispatch, "_dispatch_via_azure_sdk"))
        self.assertTrue(hasattr(job_dispatch, "_azure_settings"))

    def test_azure_env_alone_still_selects_the_azure_backend(self):
        self._set(AZURE_ENV)
        seen = {}

        def _fake_azure(upload_id, mode, *cfg):
            seen["args"] = (upload_id, mode, cfg)
            return "azure-exec-1"

        with patch.object(job_dispatch, "_dispatch_via_azure_sdk", _fake_azure):
            result = job_dispatch.dispatch_material_job(9, mode="rich")

        self.assertEqual(result, "azure-exec-1")
        self.assertEqual(seen["args"][0], 9)
        self.assertEqual(seen["args"][1], "rich")


class SubprocessFallbackTests(_DispatchTestCase):

    def test_without_cloud_config_it_falls_back_to_a_local_subprocess(self):
        called = {}

        def _fake_subprocess(upload_id, mode):
            called["args"] = (upload_id, mode)
            return "local-pid-999"

        with patch.object(job_dispatch, "_dispatch_via_subprocess", _fake_subprocess):
            result = job_dispatch.dispatch_material_job(7, mode="rich")

        self.assertEqual(result, "local-pid-999")
        self.assertEqual(called["args"], (7, "rich"))

    def test_partial_ecs_config_falls_back_rather_than_half_dispatching(self):
        os.environ["ECS_CLUSTER"] = "aitutor-prod"  # task definition missing

        with patch.object(
            job_dispatch, "_dispatch_via_subprocess", lambda *a: "local-pid-1"
        ):
            self.assertEqual(job_dispatch.dispatch_material_job(7), "local-pid-1")
