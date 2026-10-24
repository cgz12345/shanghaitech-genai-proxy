import json
import os
import unittest

from tool_calling_engine import build_tools_system_prompt, extract_tool_calls
from server import (
    app,
    extract_multimodal_parts,
    get_token_expiry,
    is_tool_refusal,
    validate_remote_url,
)

# 让鉴权相关用例与本地是否配置真实凭据解耦：注入一个“语法合法且未过期”的占位 JWT
# （exp 为 2033 年，仅用于让 upstream_auth_status() 返回 valid，不发起任何网络请求）
PLACEHOLDER_JWT = "x.eyJleHAiOjIwMDAwMDAwMDB9.x"
os.environ["SHANGHAITECH_TOKEN"] = PLACEHOLDER_JWT


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Calculate an expression",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    }
]


class ToolCallingTests(unittest.TestCase):
    def test_required_choice_is_explicit_serialization(self):
        prompt = build_tools_system_prompt(TOOLS, "required", routing_only=True)
        self.assertIn("structured-output routing task", prompt)
        self.assertIn("MUST select one or more tools", prompt)
        self.assertIn("<tool_call>", prompt)

    def test_forced_function_name(self):
        prompt = build_tools_system_prompt(
            TOOLS, {"type": "function", "function": {"name": "calculator"}}
        )
        self.assertIn('MUST call the tool named "calculator"', prompt)
        legacy = build_tools_system_prompt(TOOLS, {"name": "calculator"})
        self.assertIn('MUST call the tool named "calculator"', legacy)

    def test_kimi_protocol_envelope_is_parsed(self):
        clean, calls = extract_tool_calls(
            '<tool_call>{"name":"calculator","arguments":{"expression":"17*23"}}</tool_call>'
        )
        self.assertIsNone(clean)
        self.assertEqual("calculator", calls[0]["function"]["name"])
        self.assertEqual(
            {"expression": "17*23"},
            json.loads(calls[0]["function"]["arguments"]),
        )

    def test_unescaped_windows_paths_in_agent_call_are_repaired(self):
        raw = r"""<tool_call>{"name":"Agent","arguments":{"description":"Survey puzzles","prompt":"Explore G:\trade\crypto-puzzles thoroughly. In Git Bash use /g/trade/crypto-puzzles.\nReturn paths.","workdir":"G:\trade"}}</tool_call>"""
        clean, calls = extract_tool_calls(raw)
        self.assertIsNone(clean)
        self.assertEqual("Agent", calls[0]["function"]["name"])
        arguments = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(r"G:\trade", arguments["workdir"])
        self.assertIn(r"G:\trade\crypto-puzzles", arguments["prompt"])

    def test_plain_answer_remains_plain(self):
        clean, calls = extract_tool_calls("No tool is needed.")
        self.assertEqual("No tool is needed.", clean)
        self.assertIsNone(calls)

    def test_tool_result_recovers_name_from_call_id(self):
        chat_info, messages, _, _ = extract_multimodal_parts(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": '{"expression":"17*23"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "391"},
            ]
        )
        self.assertIn("calculator", messages[-1]["content"])
        self.assertEqual(messages[-1]["content"], chat_info)

    def test_arbitrary_bearer_token_is_rejected(self):
        response = app.test_client().get(
            "/v1/models", headers={"Authorization": "Bearer definitely-wrong"}
        )
        self.assertEqual(401, response.status_code)

    def test_invalid_request_is_openai_style_error(self):
        response = app.test_client().post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-shanghaitech-genai"},
            json={"model": "Kimi-k3", "messages": []},
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual("messages", response.get_json()["error"]["param"])

    def test_private_image_url_is_blocked(self):
        with self.assertRaises(ValueError):
            validate_remote_url("http://127.0.0.1/private.png")

    def test_tool_refusal_is_detected(self):
        self.assertTrue(is_tool_refusal("这一轮我的工具执行被临时禁用了，没法列目录。"))
        self.assertTrue(
            is_tool_refusal("I can't invoke the filesystem tool in this turn.")
        )
        self.assertFalse(is_tool_refusal("The directory contains three files."))

    def test_jwt_expiry_is_read_without_exposing_token(self):
        payload = json.dumps({"exp": 2000000000}).encode()
        encoded = __import__("base64").urlsafe_b64encode(payload).decode().rstrip("=")
        self.assertEqual(2000000000, get_token_expiry(f"x.{encoded}.x"))


if __name__ == "__main__":
    unittest.main()
