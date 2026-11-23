# 模型后端（backends）

对应 ARCH §7、§7.1、§11 与 ADR-011 / 012 / 016。

## 组件

| 模块 | 内容 |
|---|---|
| `openai_compat.py` | `OpenAICompatibleBackend`：httpx 调用 `/v1/chat/completions`；传输重试（429/5xx/超时/连接错误，指数退避）；400 等不重试；`context` 永不发送；localhost 限制 |
| `cache.py` | `request_sha256`（不含 `context`、`timeout_s`）、`FileResponseCache`（`<root>/<sha[:2]>/<sha>.json`）、`CachedBackend`（`replay_only` 下未命中即 BackendError） |
| `fake.py` | `FakeBackend`：按脚本返回或抛错，用于测试 |
| `oracle.py` | `OracleBackend`（金标准完美输出，给出流水线上限）、`NoisyOracleBackend`（按比例注入 8 类错误，记录于 `injections`） |
| `profiles.py` | `get_profile(name, backend) -> RunConfig`；档位 `cpu-lite` / `cpu-std` / `gpu-std` / `gpu-ref` |

规范 JSON 与 sha256 工具在 `src/citecell/canonical.py`。

## 安全约定

- **远端后端默认禁止**（ADR-012）：`base_url` 主机不是 `localhost` / `127.0.0.1` / `::1` 时，构造即抛 `RemoteBackendRefused`，除非 `allow_remote_backend=True`。
- **缓存含正文**（ADR-011）：缓存目录必须位于被 `.gitignore` 忽略的 `outputs/` 之下（默认 `outputs/cache/llm`），永不提交。
- 日志只记录尝试次数与 HTTP 状态码，不输出正文、引文或提示词。

## 结构化输出与思考模式（待探测确认）

- `structured_output` 可选 `json_schema` / `json_object` / `none`。`json_schema` 的 `response_format` 结构默认采用 OpenAI 形式，可通过 `response_format_builder` 替换。
- 各服务端（llama.cpp server、Ollama、vLLM）对这些参数的支持与写法**未核实**。
- 思考模式的关闭方式（`chat_template_kwargs` 或 `/no_think`）同样**未核实**。

## 探测脚本

探测不进 CI，也不下载任何模型。需要用户在本地自行运行：

```bash
.venv/bin/python scripts/probe_backend.py --base-url http://127.0.0.1:8080 --model <本地模型名>
```

脚本只打印每项探测的状态（`ok`、`json_ok`、错误类别），不打印模型输出正文。远端地址需显式加 `--allow-remote`。

### 探测结果

**尚未运行。** 结果待用户在本地运行后填写于此，每个服务端一节，包括：服务端版本、支持的 `structured_output` 模式、关闭思考模式的有效写法。

## 档位（ARCH §7.1）

档位中的预算与键数均为**初始假设，待实测校准**。模型名由调用方通过 `BackendInfo.model` 传入，预设中不含任何模型下载地址或仓库名。`gpu-ref` 仅作评测参考上限，不作为默认部署档。
