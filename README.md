# 大学知识题库 API

一个可以直接跑起来的中文题库检索服务：**多源在线检索 + 本地离线兜底**，返回结构对齐 OCS 网课助手订阅格式，也能被任何 HTTP 客户端调用。

本地保留了 248 道「大学生成长」主题题（C-Eval 筛选 234 道 + 补充判断题 14 道），在线检索优先走 GitHub 开源项目 [tikuAdapter](https://github.com/DokiDoki1103/tikuAdapter) 接入的公开题库源，命中即返回，全部不中才判定为未找到。

> 演示实例：`https://a8d93c5821aaa7c93.app.workbuddy.host/?sharecode=6QCZCGX2NG`（临时部署，可能随时下线，请以自行部署为准）

## 特性

- **多源降级**：`buguake → icodef → local` 按优先级依次检索，`trace` 字段暴露每个源的命中数和最高相似度
- **离线可用**：断网或在线源超时时自动落到本地 248 题，不依赖外网
- **答案格式友好**：多选题统一输出 `A#B#D`（OCS 要求的形态），判断题输出 `正确 / 错误`
- **开箱即用**：单文件 FastAPI 服务，无需数据库，`python3 server.py` 即可
- **可视排查**：每次检索返回 `trace`，一眼看出是哪个源命中、阈值差多少

## 快速开始

```bash
git clone https://github.com/lu-kaixin/daxue-quiz-api.git
cd daxue-quiz-api
pip install -r requirements.txt
python3 server.py          # 默认 0.0.0.0:8765
```

可选环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8765` | 监听端口 |
| `PUBLIC_BASE` | 空 | 对外访问地址，用于生成订阅里的绝对 URL |
| `SHARECODE` | 空 | 访问口令，配置后所有请求需带 `?sharecode=xxx` |

验证：

```bash
curl http://127.0.0.1:8765/health
# {"ok":true,"local":248,"sources":3,"version":"2.3"}
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/search` | 检索入口，`?q=关键词` 或 `{"q":"关键词"}` |
| GET | `/search/{keyword}` | 关键词放路径，兼容不支持 query 的客户端 |
| GET | `/api/daxue/meta` | 题库元信息、来源、模块、许可 |
| GET | `/api/daxue/sources` | 各源优先级、端点、缓存状态 |
| GET | `/api/daxue/questions` | 题目列表（默认不含答案），支持 `module` `type` `q` `limit` `offset` |
| GET | `/api/daxue/questions/{id}` | 单题详情 |
| GET | `/api/daxue/random` | 随机抽题（不含答案） |
| POST | `/api/daxue/check` | 判分，传 `{"id":"...","option":1}` |
| GET | `/api/daxue/export` | 整库导出（含答案） |
| GET | `/subscribe` | OCS 订阅配置（2 条） |
| GET | `/subscribe/generic` | 通用订阅配置（`{keyword}` 占位符） |
| GET | `/health` | 健康检查 |

### 检索示例

```bash
curl -X POST http://127.0.0.1:8765/search \
  -H "Content-Type: application/json" \
  -d '{"q":"没有现实可行性的目标不算目标"}'
```

```json
{
  "code": 0,
  "msg": "ok",
  "keyword": "没有现实可行性的目标不算目标",
  "origin": "buguake",
  "score": 0.853,
  "data": {
    "question": "没有现实可行性的目标其实不算目标。",
    "answer": "正确",
    "answer_letter": "A",
    "answer_ocs": "正确",
    "type": "judge"
  },
  "trace": [
    { "source": "buguake", "hits": 10, "top": 0.853, "used": true }
  ]
}
```

未命中时返回 `code: 404`、`data: null`，客户端应据此放弃而不是把空对象当答案。

### 指定来源

```bash
curl "http://127.0.0.1:8765/search?q=关键词&source=local"    # 只查本地
curl "http://127.0.0.1:8765/search?q=关键词&source=all&count=3"  # 全源，取多条
```

`source` 可选 `auto`（默认）、`buguake`、`icodef`、`local`、`all`。

## 检索机制

| 优先级 | 来源 | 类型 | 返回 | 相似度阈值 |
|---|---|---|---|---|
| 1 | 不挂科题库源（GitHub · tikuAdapter） | 在线 | 题干 + 答案 | 0.72 |
| 2 | 言溪题库（GitHub · tikuAdapter） | 在线 | 仅答案 | 0.90 |
| 3 | 本地 C-Eval 大学知识题 | 离线 | 题干 + 答案 + 解析 | 0.72 |

言溪只回传答案、没有题干可比对，所以阈值抬到 0.90 防误配。相似度算法为 NFKC 归一化 + 去标点/停用词后的字符级匹配。

同一题干常有多个版本（`ABD`、`A,B,D`、选项原文无分隔拼接）。服务会在同分候选中优先选择字母答案，保证客户端能正确拆分多选。

## 接入 OCS 网课助手

订阅地址：`http://你的域名:8765/subscribe`，整段 JSON 粘贴到题库配置即可。也可以直接使用 [`ocs-subscription.json`](ocs-subscription.json)。

**必须先在脚本里加域名白名单**，否则请求会被 Tampermonkey 拦截：

1. Tampermonkey 面板 →「OCS 网课助手」→ 编辑
2. 在 `// ==UserScript==` 元信息块中追加：`// @connect 你的域名`
3. `Ctrl + S` 保存，刷新课程页

OCS 的字段与通用 HTTP 客户端不同，注意这几点：

| 项 | OCS 要求 |
|---|---|
| 请求体字段 | `data`（不是 `body`） |
| 题目占位符 | `${title}`（不是 `{keyword}`） |
| 请求类型 | `type: "GM_xmlhttpRequest"`，`@connect` 只对它生效 |
| 多选答案 | 必须 `A#B#D`，不能是数组或 `ABD` |

## 数据来源与许可

**代码**以 [MIT](LICENSE) 许可发布。

**题库数据** `daxue.json` 采用 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh) 许可：

- 234 道题来自 [C-Eval](https://github.com/hkust-nlp/ceval)（清华大学、上海交通大学、爱丁堡大学等机构联合发布），选取「思想道德与法治」「教育学」「教师资格」「体育与健康」四科，按 6 个成长主题筛选，剔除中小学教学情境、教育史、运动生理等无关题目；题目标题与选项保持原始文本，`id` 沿用 C-Eval 原始编号可回溯。
- 14 道补充判断题（`id` 前缀 `supplement-`），其中 10 道经公开题库源核对。
- C-Eval 仅 `dev` 划分提供官方解析，因此只有部分题目带解析，其余留空，未编造。

使用时请保留署名、注明来源、保持相同协议共享，**不得用于商业用途**。

在线检索源为第三方公开接口，本仓库不存储其数据，仅在运行时请求。这些源可能随时变更或失效，请自行评估合规性。

## 免责声明

本项目仅供学习与技术研究，用于课程练习与自测。题库答案以课程原文为准，不保证全部正确；请勿用于任何违反学校规定或学术诚信要求的场景。因使用本项目产生的后果由使用者自行承担。

## 目录结构

```
.
├── server.py                    # 单文件服务（FastAPI）
├── daxue.json                   # 本地题库，248 题
├── requirements.txt
├── ocs-subscription.json        # OCS 订阅配置示例
├── generic-subscription.json    # 通用 HTTP 客户端配置示例
├── LICENSE                      # MIT（代码）
└── README.md
```

## 已知限制

- 本地库不含时间管理、社团实践、创业理财、职业规划类题目 —— C-Eval 里没有，未用非开源来源批量填充。
- 在线源依赖第三方接口，稳定性不可控；对稳定性要求高时建议 `?source=local` 只用本地库。
- 相似度阈值按经验设定，极端表述（口语化改写、含图片的题）可能命中不到，此时返回 404 而非猜测答案。
