"""D4-NEXT-02A1 定向测试：成员与模型登记纯合同（接口文档 §13.7）。

只测纯校验，不读文件、不落盘、不联网、不触 DB / 宿主：

- ``parse_registry_json``：重复键（含嵌套）/ NaN / Infinity / 超限 / 非对象根 / 语法错误。
- ``normalize_agent_alias``：Unicode NFC + casefold，控制符拒绝。
- ``validate_agent_registration``：12 字段、enabled=false、权限、binding、路径、词表。
- ``validate_model_profile``：fixed / host-default 边界。
- 成功返回新对象且不改输入；无外部读取（用 mock 断电证明）。

表驱动、精简实现，不写镜像断言。
"""
import contextlib
import copy
import json
import unittest
from unittest import mock

from contracts.agent_registry import (
    AGENT_REGISTRATION_V1,
    MODEL_PROFILE_V1,
    normalize_agent_alias,
    parse_registry_json,
    validate_agent_registration,
    validate_model_profile,
)
from contracts.schemas import ContractError

REGISTRATION_FIELDS = (
    "schemaVersion",
    "agentId",
    "agentAlias",
    "displayName",
    "roles",
    "capabilities",
    "environmentId",
    "allowedRoots",
    "permissionCeiling",
    "modelProfileId",
    "binding",
    "enabled",
)

DIGEST = "sha256:" + "a" * 64
ROOT = "/Users/demo/.hermes/skills/conductor"
SCOPE = "/Users/demo/.hermes/skills/conductor/receipts"


def make_registration(**over):
    doc = {
        "schemaVersion": AGENT_REGISTRATION_V1,
        "agentId": "dev-wkb1",
        "agentAlias": "dev-workbuddy-1",
        "displayName": "WorkBuddy 执行成员",
        "roles": ["skill-worker"],
        "capabilities": ["read", "develop", "test"],
        "environmentId": "workbuddy-codebuddy-local",
        "allowedRoots": [ROOT],
        "permissionCeiling": {
            "writeScope": [SCOPE],
            "allowNetwork": False,
            "allowBusinessWrite": False,
            "allowGit": False,
            "allowPublish": False,
        },
        "modelProfileId": "synthetic-fixed-v41",
        "binding": {
            "environmentId": "workbuddy-codebuddy-local",
            "adapterType": "workbuddy-native",
            "connectorConfigRef": "workbuddy-connector-1",
            "executionProfileRef": "workbuddy-profile-1",
            "hostAgentId": None,
            "configDigest": DIGEST,
        },
        "enabled": False,
    }
    doc.update(over)
    return doc


def make_fixed_profile(**over):
    doc = {
        "schemaVersion": MODEL_PROFILE_V1,
        "modelProfileId": "synthetic-fixed-v41",
        "environmentId": "workbuddy-codebuddy-local",
        "strategy": "fixed",
        "provider": "synthetic-provider",
        "modelId": "deepseek-v4.1-flash",
    }
    doc.update(over)
    return doc


def make_default_profile(**over):
    doc = {
        "schemaVersion": MODEL_PROFILE_V1,
        "modelProfileId": "synthetic-host-default",
        "environmentId": "workbuddy-codebuddy-local",
        "strategy": "host-default",
    }
    doc.update(over)
    return doc


class InvalidMixin(object):
    def assert_invalid(self, fn, field):
        with self.assertRaises(ContractError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, "invalid_task")
        self.assertEqual(ctx.exception.details.get("field"), field)
        return ctx.exception


class AgentRegistrationContractTests(InvalidMixin, unittest.TestCase):
    def test_valid_registration_returns_new_copy_and_keeps_input(self):
        payload = make_registration()
        snapshot = copy.deepcopy(payload)
        result = validate_agent_registration(payload)
        # 成功不改输入
        self.assertEqual(payload, snapshot)
        # 返回新对象，附 validator 生成的 normalizedAlias
        self.assertIsNot(result, payload)
        self.assertEqual(result["normalizedAlias"], "dev-workbuddy-1")
        self.assertEqual(result["agentAlias"], "dev-workbuddy-1")
        self.assertEqual(result["enabled"], False)

    def test_empty_capabilities_and_null_host_agent_allowed(self):
        payload = make_registration(capabilities=[])
        self.assertEqual(validate_agent_registration(payload)["capabilities"], [])
        payload = make_registration()
        payload["binding"]["executionProfileRef"] = None
        payload["binding"]["hostAgentId"] = "host-agent-9"
        result = validate_agent_registration(payload)
        self.assertIsNone(result["binding"]["executionProfileRef"])

    def test_each_required_field_missing_rejected(self):
        for field in REGISTRATION_FIELDS:
            with self.subTest(field=field):
                doc = make_registration()
                del doc[field]
                self.assert_invalid(lambda d=doc: validate_agent_registration(d), field)

    def test_each_field_wrong_type_rejected(self):
        wrong = {
            "schemaVersion": 1,
            "agentId": 123,
            "agentAlias": 5,
            "displayName": None,
            "roles": "skill-worker",
            "capabilities": {},
            "environmentId": [],
            "allowedRoots": ROOT,
            "permissionCeiling": [],
            "modelProfileId": True,
            "binding": "x",
            "enabled": "false",
        }
        for field, value in wrong.items():
            with self.subTest(field=field):
                self.assert_invalid(
                    lambda f=field, v=value: validate_agent_registration(make_registration(**{f: v})),
                    field,
                )

    def test_unknown_fields_and_nested_unknown_rejected(self):
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(extraNote="x")), "extraNote"
        )
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(normalizedAlias="dev-workbuddy-1")),
            "normalizedAlias",
        )
        doc = make_registration()
        doc["permissionCeiling"]["extra"] = True
        self.assert_invalid(lambda: validate_agent_registration(doc), "permissionCeiling.extra")
        doc = make_registration()
        doc["binding"]["verified"] = True
        self.assert_invalid(lambda: validate_agent_registration(doc), "binding.verified")

    def test_enabled_must_be_strictly_false(self):
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(enabled=True)), "enabled"
        )
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(enabled=0)), "enabled"
        )

    def test_schema_version_exact(self):
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(schemaVersion="agent-registration.v2")),
            "schemaVersion",
        )

    def test_controlled_ids_reject_trailing_lf_and_url(self):
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(agentId="dev-wkb1\n")), "agentId"
        )
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(agentId="https://host/x")), "agentId"
        )
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(modelProfileId="a/b")),
            "modelProfileId",
        )
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(environmentId="e nv")),
            "environmentId",
        )

    def test_alias_nfc_and_casefold(self):
        # NFD "e" + combining acute -> NFC "é"，同时大小写折叠
        self.assertEqual(normalize_agent_alias("DEV-cafe\u0301-1"), "dev-caf\u00e9-1")
        self.assertEqual(normalize_agent_alias("  DEV-QCLaw-1  "), "dev-qclaw-1")
        out = validate_agent_registration(make_registration(agentAlias="DEV-QCLaw-1"))
        # 输入 alias 原值保留，normalizedAlias 单独生成
        self.assertEqual(out["agentAlias"], "DEV-QCLaw-1")
        self.assertEqual(out["normalizedAlias"], "dev-qclaw-1")

    def test_alias_rejects_control_and_blank(self):
        for value in ("dev\u0000-1", "dev\u200b-1", "   ", ""):
            with self.subTest(value=repr(value)):
                self.assert_invalid(lambda v=value: normalize_agent_alias(v), "agentAlias")
                self.assert_invalid(
                    lambda v=value: validate_agent_registration(make_registration(agentAlias=v)),
                    "agentAlias",
                )

    def test_display_name_whitespace_only_rejected(self):
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(displayName="   ")), "displayName"
        )

    def test_roles_vocabulary_and_duplicates(self):
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(roles=[])), "roles"
        )
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(roles=["ceo"])), "roles[0]"
        )
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(roles=["skill-worker", "skill-worker"])),
            "roles[1]",
        )

    def test_capabilities_vocabulary_and_duplicates(self):
        self.assert_invalid(
            lambda: validate_agent_registration(
                make_registration(capabilities=["read", "read"])
            ),
            "capabilities[1]",
        )
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(capabilities=["admin"])),
            "capabilities[0]",
        )

    def test_allowed_roots_rules(self):
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(allowedRoots=[])), "allowedRoots"
        )
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(allowedRoots=[ROOT, ROOT])),
            "allowedRoots[1]",
        )
        for bad in ("relative/path", "/a/../b", "/a/./b", "/a//b", "/a/"):
            with self.subTest(bad=bad):
                self.assert_invalid(
                    lambda b=bad: validate_agent_registration(make_registration(allowedRoots=[b])),
                    "allowedRoots[0]",
                )

    def test_write_scope_path_segment_boundary(self):
        # 前缀碰撞：/a2 不能借 /a 通过
        doc = make_registration(allowedRoots=["/a"])
        doc["permissionCeiling"]["writeScope"] = ["/a2"]
        self.assert_invalid(
            lambda: validate_agent_registration(doc), "permissionCeiling.writeScope[0]"
        )
        # 空 writeScope 合法
        doc = make_registration()
        doc["permissionCeiling"]["writeScope"] = []
        self.assertEqual(validate_agent_registration(doc)["permissionCeiling"]["writeScope"], [])

    def test_permission_ceiling_missing_and_unknown(self):
        doc = make_registration()
        del doc["permissionCeiling"]["allowGit"]
        self.assert_invalid(lambda: validate_agent_registration(doc), "permissionCeiling.allowGit")
        doc = make_registration()
        doc["permissionCeiling"]["allowRoot"] = True
        self.assert_invalid(lambda: validate_agent_registration(doc), "permissionCeiling.allowRoot")

    def test_permission_toggles_strict_bool_and_forbidden_true(self):
        for key in ("allowNetwork", "allowBusinessWrite", "allowGit", "allowPublish"):
            with self.subTest(key=key):
                doc = make_registration()
                doc["permissionCeiling"][key] = "false"
                self.assert_invalid(
                    lambda d=doc, k=key: validate_agent_registration(d),
                    "permissionCeiling.%s" % key,
                )
        for key in ("allowBusinessWrite", "allowGit", "allowPublish"):
            with self.subTest(key=key):
                doc = make_registration()
                doc["permissionCeiling"][key] = True
                self.assert_invalid(
                    lambda d=doc, k=key: validate_agent_registration(d),
                    "permissionCeiling.%s" % key,
                )
        # allowNetwork=true 只声明管理上限，允许
        doc = make_registration()
        doc["permissionCeiling"]["allowNetwork"] = True
        self.assertTrue(validate_agent_registration(doc)["permissionCeiling"]["allowNetwork"])

    def test_binding_rules(self):
        doc = make_registration()
        doc["binding"]["environmentId"] = "other-env"
        self.assert_invalid(lambda: validate_agent_registration(doc), "binding.environmentId")

        doc = make_registration()
        doc["binding"]["executionProfileRef"] = None
        doc["binding"]["hostAgentId"] = None
        self.assert_invalid(lambda: validate_agent_registration(doc), "binding")

        doc = make_registration()
        del doc["binding"]["configDigest"]
        self.assert_invalid(lambda: validate_agent_registration(doc), "binding.configDigest")

    def test_config_digest_exact_sha256_rejects_trailing_lf(self):
        for bad in (DIGEST + "\n", "sha256:" + "A" * 64, "sha256:short", DIGEST.strip() + " "):
            with self.subTest(bad=repr(bad)):
                doc = make_registration()
                doc["binding"]["configDigest"] = bad
                self.assert_invalid(
                    lambda d=doc: validate_agent_registration(d), "binding.configDigest"
                )

    def test_binding_adapter_and_refs(self):
        doc = make_registration()
        doc["binding"]["adapterType"] = "http://x"
        self.assert_invalid(lambda: validate_agent_registration(doc), "binding.adapterType")
        doc = make_registration()
        doc["binding"]["executionProfileRef"] = "profile with space"
        self.assert_invalid(lambda: validate_agent_registration(doc), "binding.executionProfileRef")
        doc = make_registration()
        doc["binding"]["argv"] = ["--model", "x"]
        self.assert_invalid(lambda: validate_agent_registration(doc), "binding.argv")

    # --- 02A1-R1 新增回归：nested unknown 完整 field 路径 ---
    def test_nested_unknown_field_keeps_full_path(self):
        # nested unknown 返回完整点路径（cox probe：nested-binding-unknown）
        doc = make_registration()
        doc["binding"]["argv"] = ["/tmp/tool"]
        self.assert_invalid(lambda: validate_agent_registration(doc), "binding.argv")

        doc = make_registration()
        doc["permissionCeiling"]["extra"] = True
        self.assert_invalid(lambda: validate_agent_registration(doc), "permissionCeiling.extra")

        doc = make_registration()
        doc["permissionCeiling"]["allowRoot"] = True
        self.assert_invalid(lambda: validate_agent_registration(doc), "permissionCeiling.allowRoot")

        # 根级 unknown 仍保留原 key，不加前缀
        self.assert_invalid(
            lambda: validate_agent_registration(make_registration(extraNote="x")), "extraNote"
        )

    # --- 02A1-R1 新增回归：writeScope 重复元素按通用列表规则拒绝 ---
    def test_write_scope_duplicate_rejected_with_index_field(self):
        doc = make_registration()
        doc["permissionCeiling"]["writeScope"] = [SCOPE, SCOPE]
        self.assert_invalid(
            lambda: validate_agent_registration(doc), "permissionCeiling.writeScope[1]"
        )


class ModelProfileContractTests(InvalidMixin, unittest.TestCase):
    def test_valid_fixed_and_default(self):
        fixed = make_fixed_profile()
        snapshot = copy.deepcopy(fixed)
        result = validate_model_profile(fixed)
        self.assertEqual(fixed, snapshot)
        self.assertIsNot(result, fixed)
        self.assertEqual(result["strategy"], "fixed")
        self.assertEqual(validate_model_profile(make_default_profile())["strategy"], "host-default")

    def test_required_base_fields(self):
        for field in ("schemaVersion", "modelProfileId", "environmentId", "strategy"):
            with self.subTest(field=field):
                doc = make_default_profile()
                del doc[field]
                self.assert_invalid(lambda d=doc: validate_model_profile(d), field)

    def test_schema_and_strategy_enum(self):
        self.assert_invalid(
            lambda: validate_model_profile(make_default_profile(schemaVersion="model-profile.v2")),
            "schemaVersion",
        )
        self.assert_invalid(
            lambda: validate_model_profile(make_default_profile(strategy="auto")), "strategy"
        )

    def test_fixed_requires_provider_and_model_id(self):
        for field in ("provider", "modelId"):
            with self.subTest(field=field):
                doc = make_fixed_profile()
                del doc[field]
                self.assert_invalid(lambda d=doc: validate_model_profile(d), field)
        self.assert_invalid(
            lambda: validate_model_profile(make_fixed_profile(provider=7)), "provider"
        )
        self.assert_invalid(
            lambda: validate_model_profile(make_fixed_profile(modelId="")), "modelId"
        )

    def test_host_default_forbids_provider_and_model_id_including_null(self):
        self.assert_invalid(
            lambda: validate_model_profile(make_default_profile(provider="synthetic-provider")),
            "provider",
        )
        self.assert_invalid(
            lambda: validate_model_profile(make_default_profile(modelId=None)), "modelId"
        )

    def test_unknown_field_and_self_reported_revision_rejected(self):
        self.assert_invalid(
            lambda: validate_model_profile(make_default_profile(profileRevision=1)), "profileRevision"
        )
        self.assert_invalid(
            lambda: validate_model_profile(make_fixed_profile(endpoint="https://x")), "endpoint"
        )
        self.assert_invalid(
            lambda: validate_model_profile(make_fixed_profile(apiKey="secret")), "apiKey"
        )

    def test_ids_reject_trailing_lf(self):
        self.assert_invalid(
            lambda: validate_model_profile(make_default_profile(modelProfileId="p1\n")),
            "modelProfileId",
        )


class ParseRegistryJsonTests(InvalidMixin, unittest.TestCase):
    def test_valid_object_parsed(self):
        parsed = parse_registry_json(json.dumps(make_registration()))
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["agentId"], "dev-wkb1")

    def test_non_string_input_rejected(self):
        self.assert_invalid(lambda: parse_registry_json(b"{}"), "text")
        self.assert_invalid(lambda: parse_registry_json(None), "text")

    def test_duplicate_key_top_and_nested_rejected(self):
        self.assert_invalid(
            lambda: parse_registry_json('{"agentId": "a", "agentId": "b"}'), "agentId"
        )
        self.assert_invalid(
            lambda: parse_registry_json('{"binding": {"hostAgentId": 1, "hostAgentId": 2}}'),
            "hostAgentId",
        )

    def test_nan_and_infinity_rejected(self):
        for text in ('{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}'):
            with self.subTest(text=text):
                self.assert_invalid(lambda t=text: parse_registry_json(t), "$")

    def test_incomplete_json_rejected(self):
        self.assert_invalid(lambda: parse_registry_json('{"agentId": '), "text")

    def test_root_must_be_object(self):
        self.assert_invalid(lambda: parse_registry_json("[1, 2, 3]"), "$")
        self.assert_invalid(lambda: parse_registry_json('"agent"'), "$")

    def test_oversized_text_rejected(self):
        self.assert_invalid(
            lambda: parse_registry_json(" " * (1024 * 1024 + 1)), "text"
        )

    # --- 02A1-R1 新增回归：UTF-8 编码异常受控 ---
    def test_unpaired_surrogate_utf8_encoding_controlled(self):
        # 未配对代理项无法以 UTF-8 编码：受控 invalid_task，稳定 field/reason 且不回显输入
        text = '{"x": "' + "\ud800" + '"}'
        err = self.assert_invalid(lambda: parse_registry_json(text), "text")
        self.assertEqual(err.code, "invalid_task")
        self.assertTrue(err.details.get("reason"))
        # 不回显原始输入（含未配对代理项）
        self.assertNotIn("\ud800", str(err))

    def test_normal_utf8_round_trip_preserved(self):
        # 正常多字节 UTF-8 仍逐字节保真，未被 replace / ignore
        parsed = parse_registry_json('{"k": "caf\u00e9\u4e2d\u6587"}')
        self.assertEqual(parsed["k"], "caf\u00e9\u4e2d\u6587")

    # --- 02A1-R1 新增回归：极深 JSON 递归异常受控 ---
    def test_deep_json_recursion_controlled(self):
        deep = '{"x":' * 1100 + "0" + "}" * 1100
        self.assert_invalid(lambda: parse_registry_json(deep), "text")

    def test_parse_failure_reasons_are_distinguished(self):
        # 编码 / 递归 / 语法三类失败原因必须细分（不共用同一 reason）
        encoding = self.assert_invalid(
            lambda: parse_registry_json('{"x": "' + "\ud800" + '"}'), "text"
        )
        recursion = self.assert_invalid(
            lambda: parse_registry_json('{"x":' * 1100 + "0" + "}" * 1100), "text"
        )
        syntax = self.assert_invalid(lambda: parse_registry_json('{"agentId": '), "text")
        reasons = {
            encoding.details["reason"],
            recursion.details["reason"],
            syntax.details["reason"],
        }
        self.assertEqual(len(reasons), 3)


class NoExternalReadTests(unittest.TestCase):
    def test_validators_have_no_fs_env_db_network_host_side_effects(self):
        boom = "纯函数不应触发外部读取"
        patches = [
            mock.patch("builtins.open", side_effect=AssertionError(boom)),
            mock.patch("os.stat", side_effect=AssertionError(boom)),
            mock.patch("os.lstat", side_effect=AssertionError(boom)),
            mock.patch("os.listdir", side_effect=AssertionError(boom)),
            mock.patch("os.scandir", side_effect=AssertionError(boom)),
            mock.patch("os.getenv", side_effect=AssertionError(boom)),
            mock.patch("socket.socket", side_effect=AssertionError(boom)),
            mock.patch("sqlite3.connect", side_effect=AssertionError(boom)),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            self.assertEqual(
                validate_agent_registration(make_registration())["agentId"], "dev-wkb1"
            )
            self.assertEqual(
                validate_model_profile(make_fixed_profile())["strategy"], "fixed"
            )
            self.assertEqual(normalize_agent_alias("DEV-1"), "dev-1")
            self.assertEqual(
                parse_registry_json('{"agentId": "dev-wkb1"}')["agentId"], "dev-wkb1"
            )


if __name__ == "__main__":
    unittest.main()
