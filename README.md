# ShanghaiTech GenAI → OpenAI 兼容 API 代理

把上海科技大学校内 GenAI 服务（`https://genai.shanghaitech.edu.cn`）转换为标准的 **OpenAI 兼容 HTTP API**，从而可以在任意支持 OpenAI 协议的客户端中使用：ChatBox、NextChat、Cherry Studio、Cursor、Continue、沉浸式翻译、Dify、One-API……

> ⚠️ **免责声明**：本项目仅供上海科技大学在校师生，用于将**本人账号**的合法访问能力接入自己常用的客户端，方便自用。请遵守学校《生成式人工智能使用指南》及相关法律法规，不得转借非学校用户使用，不得用于任何商业用途。使用者需自行承担使用产生的全部责任。

## 特性

- **完整 OpenAI Chat Completions**：流式（SSE）与非流式，`/v1/models` 动态枚举。
- **Agent 工具调用（Function Calling）**：支持 `tools` / `tool_choice` 与旧版 `functions` / `function_call`，包括流式 `delta.tool_calls` 与工具结果回传；可把网页模型生成的各种工具调用格式（含 Qwen 私有标记）统一转换为标准结构。
- **多模态**：图片理解（`image_url`，支持 Base64 与远程 URL）、PDF 文档问答（`type: file`）。
- **额外入口**：文生图 `/v1/images/generations`、文字转语音 `/v1/audio/speech`、语音转文字 `/v1/audio/transcriptions`。
- **零配置刷新凭据**：`get_token.bat` 自动打开独立浏览器窗口，登录后自动提取并验证凭据，无需 F12。
- **生产级服务**：Waitress WSGI + 智能端口检测（占用时自动避让并在启动窗口如实显示）。
- **安全默认值**：远程图片默认禁止访问本机/内网地址；请求体与图片大小有上限；上游认证失效时返回明确的 HTTP 503 而不是空回复。

## 快速开始

### 1. 安装依赖
需要 Python 3.10+。双击 **`install_deps.bat`**（或运行 `pip install -r requirements.txt`）。

### 2. 获取你自己的凭据
双击 **`get_token.bat`**：程序会自动打开一个独立的 Chrome/Edge 窗口，用学校统一身份认证登录后，凭据会自动保存到 `token.txt`（不会上传到任何地方）。
若自动模式不可用，运行 `get_token.bat --manual` 手工粘贴，或按 F12 → Console 执行：

```javascript
copy(JSON.parse(localStorage.getItem('pro__Access-Token')).value)
```

### 3. 启动服务
双击 **`start.bat`**。启动窗口会实时显示：

- 本机接口地址（如 `http://127.0.0.1:8000/v1`，端口冲突时自动避让并如实显示）
- 局域网地址（同网络下手机/其他电脑可直连）
- 当前可用模型 ID 列表（标注识图 / 思考能力）与凭据状态

### 4. 配置客户端

| 配置项 | 填写内容 |
| :--- | :--- |
| 接口类型 | OpenAI API |
| API 地址 (Base URL) | `http://127.0.0.1:8000/v1`（以启动窗口显示为准） |
| API Key | `sk-shanghaitech-genai`（可用环境变量 `PROXY_API_KEY` 修改） |
| 模型名称 | 启动窗口中列出的模型 ID，如 `gpt-6-astra` |

## API 示例

```bash
# 普通对话
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-shanghaitech-genai" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-6-astra","messages":[{"role":"user","content":"你好"}]}'

# 流式
  -d '{"model":"gpt-6-astra","stream":true,"messages":[...]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-shanghaitech-genai")
resp = client.chat.completions.create(
    model="gpt-6-astra",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

识图（OpenAI Vision 格式）：

```python
client.chat.completions.create(
    model="gpt-6-astra",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "这张图是什么？"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
    ]}],
)
```

## 运维

- **查看状态**：`GET /health`，其中 `upstream_auth` 应为 `valid`；若为 `expired` / `missing`，运行 `get_token.bat` 更新凭据后重启。
- **模型列表**：`GET /v1/models`。
- **指定端口**：`start.bat 8080` 或环境变量 `PORT`。
- **可调环境变量**：`PROXY_API_KEY`（访问密钥）、`PROXY_MAX_REQUEST_BYTES`（请求体上限，默认 32 MiB）、`PROXY_MAX_IMAGE_BYTES`（远程图片上限，默认 20 MiB）、`ALLOW_PRIVATE_IMAGE_URLS=1`（允许远程图片访问内网，默认禁止）。
- **回归测试**：`python test_tool_calling.py`（协议、鉴权、参数校验与安全边界的离线测试）。

## 文件结构

| 文件 | 说明 |
| :--- | :--- |
| `server.py` | 代理服务主程序（Waitress + Flask） |
| `tool_calling_engine.py` | 工具调用协议转换引擎 |
| `get_token.py` / `get_token.bat` | 凭据自动获取工具 |
| `start.bat` / `install_deps.bat` | 一键启动 / 一键装依赖 |
| `token.txt` | **你的**登录凭据（首次获取时生成；已在 `.gitignore` 中，绝不会提交） |
| `SECURITY.md` | 部署、凭据与网络安全说明 |
| `test_tool_calling.py` | 离线回归测试 |

## 安全与隐私

- 所有请求由你本机直接发往学校服务器，不经过任何第三方中转；凭据仅保存在本机 `token.txt`。
- 服务默认监听 `0.0.0.0`（局域网可访问）。**建议设置强随机 `PROXY_API_KEY`**，不要暴露到公网。
- 详见 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE)
