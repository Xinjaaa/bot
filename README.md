# WeCom Callback Agent

一个基于 FastAPI 的企业微信应用回调服务。

它支持：

- 企业微信回调 URL 验证
- 企业微信消息验签与 AES 解密
- 收到文本消息后异步调用大模型
- 通过企业微信应用消息接口主动把模型回复发回给用户
- 基于 `MsgId` 的短期去重，避免企业微信重试造成重复回复
- 通过 Markdown 文件管理系统提示词
- 为每个企业微信用户自动维护一个 `<User>-Identity.md` 身份档案

## 项目结构

```text
app/
  agent.py         OpenAI-compatible Agent 调用
  crypto.py        企业微信回调消息验签与 AES 加解密
  dedupe.py        短期消息去重
  identity.py      用户身份档案读写与提取
  main.py          FastAPI 入口
identities/        自动生成的用户身份档案
  wecom_api.py     企业微信 access_token 和主动发消息接口
prompts/
  system_prompt.md 系统提示词
scripts/
  deploy_tx.sh     部署到 tx 的脚本
```

## 工作流程

1. 企业微信把消息回调到 `POST /wecom/callback`
2. 服务校验 `msg_signature`，解密 `Encrypt`
3. 解析消息内容并用 `MsgId` 去重
4. 立即返回 `success`，避免企业微信回调超时
5. 更新用户身份档案与会话记忆
6. 后台异步调用模型生成回复
7. 调用企业微信 `message/send` 主动把回复发给用户

## 环境变量

参考 [`.env.example`](.env.example)。

### 企业微信回调配置

- `WECOM_TOKEN`: 企业微信回调 Token
- `WECOM_ENCODING_AES_KEY`: 企业微信回调 EncodingAESKey
- `WECOM_CORP_ID`: 企业微信 CorpID

### 企业微信主动发消息配置

- `WECOM_APP_SECRET`: 自建应用 Secret，用于获取 `access_token`
- `WECOM_AGENT_ID`: 自建应用 AgentId
- `WECOM_API_BASE_URL`: 企业微信 API 地址，默认 `https://qyapi.weixin.qq.com`
- `WECOM_API_TIMEOUT_SECONDS`: 企业微信接口调用超时，默认 `10`
- `WECOM_MAX_TEXT_BYTES`: 主动发送文本消息的最大字节数，默认 `1800`
- `MESSAGE_DEDUPE_TTL_SECONDS`: 消息去重窗口，默认 `600`

### 模型配置

- `OPENAI_BASE_URL`: OpenAI-compatible 接口地址
- `OPENAI_API_KEY`: 模型密钥
- `OPENAI_MODEL`: 模型名
- `OPENAI_SYSTEM_PROMPT_FILE`: 系统提示词文件路径，默认 `/app/prompts/system_prompt.md`
- `OPENAI_SYSTEM_PROMPT`: 可选。如果设置，优先覆盖文件中的提示词
- `OPENAI_TIMEOUT_SECONDS`: 模型超时，默认 `20`

### 会话记忆配置

- `MEMORY_ENABLED`: 是否启用轻量会话记忆，默认 `true`
- `MEMORY_MAX_TURNS`: 每个用户保留的最近轮次，默认 `6`
- `MEMORY_TTL_SECONDS`: 会话记忆 TTL，默认 `1800`

### 用户身份档案配置

- `IDENTITY_DIR`: 用户身份档案目录，默认 `/app/identities`

### 日志

- `LOG_LEVEL`: 默认 `INFO`

## 本地运行

1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. 配置环境变量

```bash
cp .env.example .env
export $(grep -v '^#' .env | xargs)
```

3. 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Docker 运行

```bash
docker build -t wecom-callback:latest .
docker run -d \
  --name wecom-callback \
  -p 8000:8000 \
  --env-file .env \
  --restart unless-stopped \
  wecom-callback:latest
```

## 企业微信后台配置

在企业微信应用后台填写：

- URL: `http://<your-host>:8000/wecom/callback`
- Token: 使用 `WECOM_TOKEN`
- EncodingAESKey: 使用 `WECOM_ENCODING_AES_KEY`

如果是你当前部署在 `tx` 的实例，回调地址就是：

`http://43.167.159.220:8000/wecom/callback`

## 提示词管理

默认提示词文件是 [`prompts/system_prompt.md`](prompts/system_prompt.md)。

如果希望使用 Markdown 文件中的提示词：

- 保留 `OPENAI_SYSTEM_PROMPT_FILE=/app/prompts/system_prompt.md`
- 不要在 `.env` 中设置 `OPENAI_SYSTEM_PROMPT`

如果同时设置了二者，代码会优先使用 `OPENAI_SYSTEM_PROMPT`。

## 部署到 tx

初始化部署：

```bash
./scripts/deploy_tx.sh
```

远端部署目录：

```text
/opt/wecom-callback
```

如果只是修改了代码或提示词，也可以手动在远端重建并重启容器。

如果希望身份档案在重启后依然保留，建议把宿主机目录挂载到 `/app/identities`。

## 调试

查看远端实时日志：

```bash
ssh tx 'docker logs -f wecom-callback'
```

健康检查：

```bash
curl http://127.0.0.1:8000/healthz
```

示例返回：

```json
{"status":"ok","agent_configured":"true","wecom_api_configured":"true"}
```

## 注意事项

- 企业微信回调必须尽快返回，所以这里采用“异步生成 + 主动发送”的设计。
- 回复发送前会按 UTF-8 字节长度截断，避免消息过长。
- 项目当前主要处理文本消息；非文本消息和事件默认返回 `success`。
- 当前实现依赖 OpenAI-compatible `Responses API`。
- 当前记忆是进程内内存版，适合单实例调试和轻量场景；如果后面要多实例或长期记忆，建议换成 Redis 或数据库。
- 发送 `重置`、`清空记忆`、`清除记忆` 或 `/reset` 可以清掉当前用户的会话记忆。
- 身份档案只保存“用户明确自述”的事实，例如姓名、公司、职位、城市、学校；模糊推断不会写入档案。
- 每个用户会生成一个 `identities/<User>-Identity.md` 文件，并在回复时作为身份上下文注入给模型。
