"""
题库服务（多源 · 大学知识题）

设计原则
    1. 优先外部题库源（GitHub 项目 tikuAdapter 接入的两个公开源），本地库兜底；
    2. 本地库只保留「大学知识」部分：从 C-Eval 开源数据集筛选的 234 道题
       + 经题库源核对的 14 道判断题，共 248 道；
    3. 不再提供 C-Eval 52 学科全量题库（已下线，数据文件移出服务目录）。

来源优先级
    1  buguake  不挂科题库源   GitHub · tikuAdapter   在线，回传题干 + 答案
    2  icodef   言溪题库       GitHub · tikuAdapter   在线，仅回传答案
    3  local    C-Eval 大学知识题                       本地 248 道，离线可用

启动：
    cd /workspace/api-quiz-service
    python3 server.py                 # 默认 0.0.0.0:8765
    PORT=3000 python3 server.py

接口：
    GET  /                          服务说明（JSON）
    GET  /health                    健康检查
    GET/POST /search                ★ 检索入口（OCS 订阅使用）
    GET  /api/daxue/meta            概览：来源、模块、许可
    GET  /api/daxue/sources         多源状态与优先级
    GET  /api/daxue/questions       题目列表（不含答案）
    GET  /api/daxue/questions/{id}  单题详情（不含答案）
    GET  /api/daxue/random          随机抽题（不含答案）
    POST /api/daxue/check           判分
    GET  /api/daxue/export          整库导出（含答案）
    GET  /subscribe                 ★ OCS 订阅配置（JSON 数组）
    GET  /subscribe/single          单条订阅配置
"""

import json
import os
import random
import re
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent

# 可选部署配置：与 server.py 同级的 config.json（不进版本库），
# 用于固定对外地址与访问口令。环境变量优先于文件。
_DEPLOY_CFG: dict = {}
_cfg_path = BASE / "config.json"
if _cfg_path.exists():
    try:
        _DEPLOY_CFG = json.loads(_cfg_path.read_text(encoding="utf-8"))
    except Exception:
        _DEPLOY_CFG = {}

PUBLIC_BASE = os.environ.get("PUBLIC_BASE", _DEPLOY_CFG.get("publicBase", "")).rstrip("/")
PUBLIC_HOSTS = tuple(_DEPLOY_CFG.get("publicHosts") or ())
DEFAULT_SHARECODE = os.environ.get("SHARECODE", _DEPLOY_CFG.get("sharecode", "")).strip()
LISTEN_PORT = int(os.environ.get("PORT", "8765"))

# ---------------- 本地题库（C-Eval 大学知识部分） ----------------
DX_DATA = json.loads((BASE / "daxue.json").read_text(encoding="utf-8"))
DX_QUESTIONS = DX_DATA["questions"]
DX_BY_ID = {q["id"]: q for q in DX_QUESTIONS}

PROVIDER = {
    "name": "大学知识题库（多源版）",
    "alias": "《大学启示录：如何读大学？》等大学成长类基础题",
    "version": DX_DATA["version"],
    "description": "多源检索：优先 GitHub 公开题库源，本地保留 {} 道大学知识题（C-Eval 筛选 234 道 + 补充判断 14 道）。".format(len(DX_QUESTIONS)),
    "author": "主体题目来自 C-Eval 开源数据集（按主题筛选重组），补充判断题经 GitHub tikuAdapter 接入的公开题库核对",
    "homepage": DX_DATA["source"]["site"],
    "dataset": DX_DATA["source"]["dataset"],
    "dataset_url": DX_DATA["source"]["dataset_url"],
    "license": DX_DATA["source"]["license"],
    "license_url": DX_DATA["source"]["license_url"],
    "update_time": "2026-11-17",
    "contact": "数据问题请参考 C-Eval 官方仓库 github.com/hkust-nlp/ceval",
}

# ---------------- 来源定义 ----------------
SOURCES = [
    {
        "id": "buguake",
        "name": "不挂科题库源",
        "via": "GitHub · DokiDoki1103/tikuAdapter",
        "repo": "https://github.com/DokiDoki1103/tikuAdapter",
        "priority": 1,
        "online": True,
        "endpoint": "https://easylearn.baidu.com/edu-web-go/bgk/searchlist",
        "returns": "题干 + 答案",
        "note": "第三方公开题库，按题干检索，可回传完整题干用于相似度校验。",
    },
    {
        "id": "icodef",
        "name": "言溪题库",
        "via": "GitHub · DokiDoki1103/tikuAdapter",
        "repo": "https://github.com/DokiDoki1103/tikuAdapter",
        "priority": 2,
        "online": True,
        "endpoint": "https://cx.icodef.com/wyn-nb?v=4",
        "returns": "仅答案",
        "note": "同项目接入的第二个源；只回传答案不回传题干，无法做相似度校验，故排在第二。",
    },
    {
        "id": "local",
        "name": "C-Eval 大学知识题",
        "via": "本地库 · CC BY-NC-SA 4.0",
        "repo": "https://github.com/hkust-nlp/ceval",
        "priority": 3,
        "online": False,
        "endpoint": "",
        "returns": "题干 + 答案 + 解析",
        "count": len(DX_QUESTIONS),
        "note": "从 C-Eval 52 学科中筛出的大学成长主题题，离线可用。",
    },
]
SOURCE_BY_ID = {s["id"]: s for s in SOURCES}
DEFAULT_ORDER = [s["id"] for s in sorted(SOURCES, key=lambda x: x["priority"])]

# ---------------- 相似度 ----------------
_PUNC = re.compile(r"[\s\u3000，。、；：？！,.;:?!\"'“”‘’（）()《》\[\]【】<>·…—\-_/\\|]+")
_STOP = (
    "请问", "下列", "以下", "关于", "什么", "哪个", "哪些", "哪项", "正确", "错误",
    "说法", "的是", "不是", "属于", "可以", "应该", "如何", "怎样", "为什么", "吗", "呢",
)


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    s = re.sub(r"^\s*(?:第?\d+[题、.．)）]|\(\d+\)|（\d+）)\s*", "", s)
    s = _PUNC.sub("", s)
    for w in _STOP:
        s = s.replace(w, "")
    return s


def bigrams(s: str) -> set:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i: i + 2] for i in range(len(s) - 1)}


def score(query: str, target: str) -> float:
    """0~1 相似度：包含关系优先，其次字符二元组重合度。"""
    a, b = normalize(query), normalize(target)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b:
        return 0.92 + 0.08 * min(1.0, len(a) / max(len(b), 1))
    if b in a and len(b) >= 6:
        return 0.85
    ga, gb = bigrams(a), bigrams(b)
    inter = len(ga & gb)
    if not inter:
        return 0.0
    jac = inter / len(ga | gb)
    cover = inter / len(ga)
    return 0.65 * cover + 0.35 * jac


# ---------------- 本地检索 ----------------
def local_search(keyword: str, limit: int = 3) -> list:
    scored = []
    for q in DX_QUESTIONS:
        s = score(keyword, q["question"])
        s = max(s, score(keyword, q["explanation"]) * 0.78)
        s = max(s, score(keyword, " ".join(q["options"])) * 0.55)
        if s > 0:
            scored.append((s, q))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


_LETTERS_RE = re.compile(r"^[A-Za-z](?:[\s,，#、/|]*[A-Za-z])+$")


def ocs_answer(answer) -> str:
    """OCS 多选题答案必须用分隔符连接，不能是数组；单选/判断保持原文。"""
    a = (answer or "").strip()
    if _LETTERS_RE.match(a):
        return "#".join(re.findall(r"[A-Za-z]", a)).upper()
    return a


def local_payload(q: dict) -> dict:
    answer = q["options"][q["answer"]] if q.get("options") else ""
    return {
        "id": q["id"],
        "question": q["question"],
        "answer": answer,
        "answer_flat": answer,
        "answer_letter": q.get("answer_letter", ""),
        "answer_text": q.get("answer_text", ""),
        "answer_ocs": ocs_answer(answer),
        "options": q.get("options", []),
        "module": q.get("module_name", ""),
        "subject": q.get("subject_cn", ""),
        "source": q.get("source") or "C-Eval",
        "source_id": "local",
        "explanation": q.get("explanation") or "C-Eval 仅在 dev 划分提供官方解析，本题所属划分未提供。",
    }


# ---------------- 源 1：不挂科 ----------------
_UP_MASK = "34cab29ef956d78afd"
_cache: dict = {}


def _up_decrypt(token: str, actk: str) -> str:
    import base64

    key = _UP_MASK + actk
    raw = base64.b64decode(token)
    buf = bytearray(len(raw))
    for i, byte in enumerate(raw):
        buf[i] = byte ^ ord(key[i % len(key)])
    chars = []
    pos = 0
    while pos < len(buf) - 1:
        chars.append(chr(buf[pos] ^ buf[pos + 1]))
        pos += 2
    return urllib.parse.unquote_plus("".join(chars))


def _node_text(node) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_node_text(x) for x in node)
    if isinstance(node, dict):
        return _node_text(node.get("c", ""))
    return ""


def buguake_search(keyword: str, timeout: int = 6) -> list:
    key = ("buguake", keyword)
    if key in _cache:
        return _cache[key]
    query = urllib.parse.urlencode({"query": keyword, "rn": 10, "pn": 0})
    req = urllib.request.Request(
        f"{SOURCE_BY_ID['buguake']['endpoint']}?{query}",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    try:
        payload = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore"))
    except Exception:
        return []
    rows = []
    for item in (payload.get("data") or {}).get("list") or []:
        try:
            node = json.loads(_up_decrypt(item["bdjson"], item["actk"]))
        except Exception:
            continue
        stem = _node_text(node.get("que_stem")).strip()
        answer = _node_text(node.get("que_answer")).strip()
        if not stem or not answer:
            continue
        rows.append(
            {
                "question": stem,
                "answer": answer,
                "options": _node_text(node.get("que_options")).strip(),
                "type": (node.get("que_info") or {}).get("type", ""),
                "score": round(score(keyword, stem), 4),
            }
        )
    rows.sort(key=lambda r: r["score"], reverse=True)
    # 同一题干常有多个版本：有的是字母答案（ABD），有的是选项原文无分隔拼接，
    # 客户端无法拆成多选。仅当首选答案确实是「长文本拼接」时，才把同题干的
    # 字母版本提到最前；判断题（答案为「正确/错误」）不受影响。
    if rows:
        top = rows[0]
        top_answer = (top["answer"] or "").strip()
        if not _LETTERS_RE.match(top_answer) and len(top_answer) > 20:
            base_q = normalize(top["question"])
            for i, r in enumerate(rows):
                if (
                    i
                    and _LETTERS_RE.match((r["answer"] or "").strip())
                    and normalize(r["question"]) == base_q
                    and top["score"] - r["score"] <= 0.05
                ):
                    rows.insert(0, rows.pop(i))
                    break
    _cache[key] = rows
    return rows


# ---------------- 源 2：言溪 ----------------
def icodef_search(keyword: str, timeout: int = 6) -> list:
    key = ("icodef", keyword)
    if key in _cache:
        return _cache[key]
    data = urllib.parse.urlencode({"question": keyword}).encode()
    req = urllib.request.Request(
        SOURCE_BY_ID["icodef"]["endpoint"],
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"},
    )
    try:
        payload = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore"))
    except Exception:
        return []
    if str(payload.get("code")) not in ("1", "0"):
        return []
    raw = payload.get("data")
    items = raw if isinstance(raw, list) else [raw]
    rows = []
    for item in items:
        if isinstance(item, dict):
            text = item.get("answer") or item.get("data") or ""
            stem = item.get("question") or item.get("title") or ""
        else:
            text, stem = str(item or ""), ""
        parts = [p.strip() for p in str(text).split("#") if p.strip()]
        if not parts:
            continue
        rows.append(
            {
                "question": stem or keyword,
                "answer": "、".join(parts),
                "options": "",
                "type": "qa",
                "score": round(score(keyword, stem), 4) if stem else 0.9,
                "no_stem": not bool(stem),
            }
        )
    _cache[key] = rows
    return rows


# ---------------- 统一检索 ----------------
def search_source(source_id: str, keyword: str) -> list:
    if source_id == "buguake":
        return buguake_search(keyword)
    if source_id == "icodef":
        return icodef_search(keyword)
    if source_id == "local":
        return [{"question": q["question"], "score": round(s, 4), "_q": q} for s, q in local_search(keyword, 5)]
    return []


def to_payload(row: dict, source_id: str) -> dict:
    if source_id == "local":
        return local_payload(row["_q"])
    answer = row["answer"]
    is_judge = answer in ("正确", "错误", "对", "错", "√", "×") or row.get("type") == "tf"
    letter = {"正确": "A", "对": "A", "√": "A", "错误": "B", "错": "B", "×": "B"}.get(answer, "")
    return {
        "id": "{}-{}".format(source_id, abs(hash(row["question"])) % 10 ** 8),
        "question": row["question"],
        "answer": answer,
        "answer_flat": answer,
        "answer_letter": letter,
        "answer_text": ("{} . {}".format(letter, answer) if letter else answer),
        "answer_ocs": ocs_answer(answer),
        "options": ["正确", "错误"] if is_judge else [],
        "type": "judge" if is_judge else "qa",
        "source": "{}（{}）".format(SOURCE_BY_ID[source_id]["via"], SOURCE_BY_ID[source_id]["name"]),
        "source_id": source_id,
        "verified": False,
        "explanation": "本题由在线题库源实时检索返回，未经本服务人工校订；结构化答案请以课程原文为准。",
    }


def multi_search(keyword: str, count: int = 1, order=None):
    """按优先级依次检索，返回 (payloads, trace, used_source)。"""
    order = order or DEFAULT_ORDER
    trace, results = [], []
    for sid in order:
        if sid not in SOURCE_BY_ID:
            continue
        rows = search_source(sid, keyword)
        top = rows[0]["score"] if rows else 0.0
        threshold = 0.9 if sid == "icodef" else 0.72
        ok = bool(rows) and top >= threshold
        trace.append(
            {
                "source": sid,
                "name": SOURCE_BY_ID[sid]["name"],
                "hits": len(rows),
                "top": round(top, 3),
                "used": ok,
            }
        )
        if ok:
            return [to_payload(r, sid) for r in rows[:count]], trace, sid
    return [], trace, None


# ---------------- FastAPI ----------------
app = FastAPI(
    title="大学知识题库 API（多源）",
    description="优先 GitHub 公开题库源，本地保留 C-Eval 筛选的 248 道大学知识题。支持 OCS 订阅检索。",
    version=PROVIDER["version"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class DxAnswerPayload(BaseModel):
    id: str
    options: list | None = None
    option: int | None = None


def pick_json_keyword(obj):
    priority = ("question", "q", "query", "keyword", "kw", "text", "title", "content", "word", "w")

    def walk(node, depth=0):
        if depth > 6:
            return None
        if isinstance(node, dict):
            for k in priority:
                v = node.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            for v in node.values():
                r = walk(v, depth + 1)
                if r:
                    return r
        elif isinstance(node, list):
            for v in node:
                r = walk(v, depth + 1)
                if r:
                    return r
        return None

    return walk(obj)


def _is_placeholder(v: str) -> bool:
    """客户端未替换模板占位符时，别把字面量当关键词。"""
    s = (v or "").strip()
    return s in ("{keyword}", "${title}", "{title}", "${keyword}", "{question}", "${question}")


async def extract_keyword(request: Request, path_kw: str) -> str:
    if path_kw:
        kw = urllib.parse.unquote(path_kw).strip()
        return "" if _is_placeholder(kw) else kw
    for key in ("q", "query", "question", "keyword", "kw", "text", "title", "w"):
        v = request.query_params.get(key)
        if v and v.strip() and not _is_placeholder(v):
            return v.strip()
    if request.method == "POST":
        raw = (await request.body()).decode("utf-8", "ignore").strip()
        if raw:
            try:
                found = pick_json_keyword(json.loads(raw))
                if found and not _is_placeholder(found):
                    return found
            except Exception:
                pass
            try:
                form = urllib.parse.parse_qs(raw)
                for key in ("q", "query", "question", "keyword", "kw", "text", "title", "w"):
                    if form.get(key) and not _is_placeholder(form[key][0]):
                        return form[key][0].strip()
            except Exception:
                pass
            if len(raw) <= 500 and not raw.startswith("{") and not _is_placeholder(raw):
                return raw
    return ""


# ---------------- 基础接口 ----------------
@app.get("/")
def root():
    return {
        "name": PROVIDER["name"],
        "version": PROVIDER["version"],
        "local": len(DX_QUESTIONS),
        "sources": [{k: s[k] for k in ("id", "name", "priority", "online")} for s in SOURCES],
        "endpoints": [
            "GET /search?q=关键词",
            "POST /search  {\"q\":\"关键词\"}",
            "GET /api/daxue/meta",
            "GET /api/daxue/sources",
            "GET /api/daxue/questions?module=&q=&limit=20",
            "GET /api/daxue/random?count=5",
            "POST /api/daxue/check",
            "GET /api/daxue/export",
            "GET /subscribe",
        ],
    }


@app.get("/health")
def health():
    return {"ok": True, "local": len(DX_QUESTIONS), "sources": len(SOURCES), "version": PROVIDER["version"]}


# ---------------- 检索入口 ----------------
@app.api_route("/search", methods=["GET", "POST", "OPTIONS"])
@app.api_route("/search/{path_kw:path}", methods=["GET", "POST", "OPTIONS"])
async def search(request: Request, path_kw: str = None):
    if request.method == "OPTIONS":
        return JSONResponse({"ok": True})

    keyword = await extract_keyword(request, path_kw)
    if not keyword:
        return {"code": 400, "msg": "缺少关键词。GET 用 ?q=关键词，POST 用 {\"q\":\"关键词\"}。", "data": None}

    try:
        count = int(request.query_params.get("count", "1"))
    except ValueError:
        count = 1
    count = max(1, min(count, 10))

    src = (request.query_params.get("source") or "auto").strip()
    if src in ("all", "*"):
        order, multi = DEFAULT_ORDER, True
    elif src in SOURCE_BY_ID:
        order, multi = [src], False
    else:
        order, multi = DEFAULT_ORDER, False

    payloads, trace, used = multi_search(keyword, count, order)

    if not payloads:
        return {"code": 404, "msg": "各题库源均未匹配到足够相似的题目", "keyword": keyword, "data": None, "trace": trace}

    top_score = next((t["top"] for t in trace if t["source"] == used), 0.0)
    body = {
        "code": 0,
        "msg": "ok",
        "keyword": keyword,
        "origin": used,
        "source_name": SOURCE_BY_ID[used]["name"],
        "score": top_score,
        "data": payloads[0],
        "trace": trace,
    }
    if count > 1 or multi:
        body["items"] = payloads
    return body


# ---------------- 题库接口 ----------------
@app.get("/api/daxue/meta")
def dx_meta():
    return {
        "name": PROVIDER["name"],
        "alias": PROVIDER["alias"],
        "version": PROVIDER["version"],
        "description": PROVIDER["description"],
        "total": len(DX_QUESTIONS),
        "source": DX_DATA["source"],
        "sources": [
            {
                "id": s["id"],
                "name": s["name"],
                "via": s["via"],
                "priority": s["priority"],
                "online": s["online"],
                "returns": s["returns"],
                "count": s.get("count"),
            }
            for s in SOURCES
        ],
        "modules": DX_DATA["modules"],
        "endpoints": [
            "GET /search?q=关键词",
            "POST /search  {\"q\":\"关键词\"}",
            "GET /api/daxue/sources",
            "GET /api/daxue/questions?module=&q=&limit=20",
            "GET /api/daxue/random?count=5",
            "POST /api/daxue/check",
            "GET /api/daxue/export",
            "GET /subscribe",
        ],
    }


@app.get("/api/daxue/sources")
def dx_sources(probe: str = Query("", description="可选：用一句话测试各源")):
    out = []
    for s in SOURCES:
        item = dict(s)
        item["cached"] = sum(1 for (sid, _) in _cache if sid == s["id"])
        if probe.strip() and s["online"]:
            rows = search_source(s["id"], probe.strip())
            item["probe"] = {"keyword": probe.strip(), "matched": bool(rows), "top": rows[0] if rows else None}
        out.append(item)
    return {"order": DEFAULT_ORDER, "sources": out}


@app.get("/api/daxue/questions")
def dx_questions(
    module: str = None,
    chapter: str = None,
    type: str = None,
    q: str = None,
    limit: int = 20,
    offset: int = 0,
):
    mod = module or chapter
    items = DX_QUESTIONS
    if mod and mod != "all":
        items = [x for x in items if x["module"] == mod]
    if type and type != "all":
        items = [x for x in items if x["type"] == type]
    if q:
        items = [x for s, x in local_search(q, 500) if s >= 0.35]
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    hide = ("answer", "answer_flat", "answer_letter", "answer_text")
    return {
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "items": [{k: v for k, v in local_payload(x).items() if k not in hide} for x in items[offset: offset + limit]],
    }


@app.get("/api/daxue/questions/{question_id}")
def dx_question(question_id: str):
    item = DX_BY_ID.get(question_id)
    if not item:
        raise HTTPException(status_code=404, detail="题目不存在")
    return local_payload(item)


@app.get("/api/daxue/random")
def dx_random(count: int = 5, module: str = None, type: str = None):
    pool = DX_QUESTIONS
    if module and module != "all":
        pool = [x for x in pool if x["module"] == module]
    if type and type != "all":
        pool = [x for x in pool if x["type"] == type]
    if not pool:
        raise HTTPException(status_code=404, detail="当前筛选没有题目")
    count = max(1, min(count, 100))
    picked = random.sample(pool, min(count, len(pool)))
    hide = ("answer", "answer_flat", "answer_letter", "answer_text")
    return {"count": len(picked), "items": [{k: v for k, v in local_payload(x).items() if k not in hide} for x in picked]}


@app.post("/api/daxue/check")
def dx_check(payload: DxAnswerPayload):
    item = DX_BY_ID.get(payload.id)
    if not item:
        raise HTTPException(status_code=404, detail="题目不存在")
    picked = payload.options if payload.options is not None else ([payload.option] if payload.option is not None else None)
    correct = sorted(picked) == [item["answer"]] if picked is not None else None
    return {
        "id": item["id"],
        "correct": correct,
        "answer_index": [item["answer"]],
        "answer_letter": item.get("answer_letter", ""),
        "answer": item["options"][item["answer"]],
        "answer_text": item.get("answer_text", ""),
        "explanation": item.get("explanation", ""),
    }


@app.get("/api/daxue/export")
def dx_export():
    return {
        "name": PROVIDER["name"],
        "version": PROVIDER["version"],
        "count": len(DX_QUESTIONS),
        "license": PROVIDER["license"],
        "sources": [{"id": s["id"], "name": s["name"], "priority": s["priority"]} for s in SOURCES],
        "questions": DX_QUESTIONS,
    }


# ---------------- OCS 订阅 ----------------
def resolve_sharecode(request: Request) -> str:
    return (request.query_params.get("sharecode") or DEFAULT_SHARECODE).strip()


def resolve_base(request: Request) -> str:
    forced = (request.query_params.get("base") or "").strip().rstrip("/")
    if forced:
        return forced
    if PUBLIC_BASE:
        return PUBLIC_BASE
    # 平台代理常把内部域名写进 X-Forwarded-Host，外部访问不到；
    # 因此只信任白名单内的 Host，其余回落到 localhost。
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    proto = (request.headers.get("x-forwarded-proto") or "http").split(",")[0].strip()
    hostname = host.split(":")[0]
    if hostname and (
        not PUBLIC_HOSTS or any(hostname.endswith(h) for h in PUBLIC_HOSTS)
    ):
        if hostname not in ("127.0.0.1", "localhost", "0.0.0.0"):
            return "{}://{}".format(proto, host)
    return "http://127.0.0.1:{}".format(LISTEN_PORT)


def _ocs_handler() -> str:
    # OCS 用 new Function(handler) 解析；答案字段优先级 answer_ocs > answer_letter > answer。
    # 未命中返回 undefined（OCS 据此视为无结果），不返回 []，否则会被当成命中。
    return (
        "return (res) => {"
        " if (!res) return undefined;"
        " const pick = (d) => [d.question, (d.answer_ocs || d.answer_letter || d.answer)];"
        " if (res.items && res.items.length) return res.items.map(pick);"
        " if (res.data && res.data.question) return [pick(res.data)];"
        " return undefined; }"
    )


def _common_meta(base: str, auth: str) -> dict:
    return {
        "author": PROVIDER["author"],
        "version": PROVIDER["version"],
        "description": PROVIDER["description"],
        "homepage": "{}/api/daxue/meta{}".format(base, ("?" + auth) if auth else ""),
        "license": PROVIDER["license"],
        "licenseUrl": PROVIDER["license_url"],
        "updateTime": PROVIDER["update_time"],
        "count": len(DX_QUESTIONS),
        "sources": [
            {"id": s["id"], "name": s["name"], "via": s["via"], "priority": s["priority"], "online": s["online"], "returns": s["returns"]}
            for s in SOURCES
        ],
        "resolution": "按优先级依次检索：1 不挂科（GitHub）→ 2 言溪（GitHub）→ 3 本地 C-Eval 大学知识题。可用 ?source=buguake|icodef|local|all 指定。",
        "modules": [{"id": m["id"], "name": m["name"], "count": m["count"]} for m in DX_DATA["modules"]],
    }


def build_subscription(base: str, sharecode: str) -> list:
    """OCS 网课助手专用格式：请求体字段是 data，占位符是 ${title}，默认请求类型是 fetch。"""
    auth = "sharecode={}".format(sharecode) if sharecode else ""
    suffix = ("?" + auth) if auth else ""
    common = _common_meta(base, auth)
    handler = _ocs_handler()
    return [
        {
            "name": "大学知识题库（OCS · 单结果）",
            "url": "{}/search{}".format(base, suffix),
            "method": "post",
            "contentType": "json",
            "type": "GM_xmlhttpRequest",
            "headers": {"Accept": "application/json", "Content-Type": "application/json"},
            "data": {"q": "${title}"},
            "handler": handler,
            **common,
        },
        {
            "name": "大学知识题库（OCS · 多结果）",
            "url": "{}/search?count=3{}".format(base, ("&" + auth) if auth else ""),
            "method": "get",
            "contentType": "json",
            "type": "GM_xmlhttpRequest",
            "headers": {"Accept": "application/json"},
            "data": {"q": "${title}"},
            "handler": handler,
            **common,
        },
    ]


def build_generic_subscription(base: str, sharecode: str) -> list:
    """通用格式：占位符为 {keyword}，同时写进 url 与 body，兼容只替换 URL 的客户端。"""
    auth = "sharecode={}".format(sharecode) if sharecode else ""
    qs_single = "q={keyword}" + ("&" + auth if auth else "")
    qs_multi = "q={keyword}&count=3" + ("&" + auth if auth else "")
    handler = (
        "return (res) => {"
        " if (!res) return undefined;"
        " const pick = (d) => [d.question, (d.answer_ocs || d.answer_letter || d.answer)];"
        " if (res.items && res.items.length) return res.items.map(pick);"
        " if (res.data && res.data.question) return [pick(res.data)];"
        " return undefined; }"
    )
    common = _common_meta(base, auth)
    return [
        {
            "name": "大学知识题库（通用 · 单结果）",
            "url": "{}/search?{}".format(base, qs_single),
            "method": "post",
            "contentType": "json",
            "headers": {"Accept": "application/json", "Content-Type": "application/json"},
            "body": {"q": "{keyword}"},
            "data": {"q": "{keyword}"},
            "handler": handler,
            **common,
        },
        {
            "name": "大学知识题库（通用 · 多结果）",
            "url": "{}/search?{}".format(base, qs_multi),
            "method": "get",
            "contentType": "json",
            "headers": {"Accept": "application/json"},
            "handler": handler,
            **common,
        },
    ]


@app.get("/subscribe")
@app.get("/subscribe.json")
@app.get("/qbank.json")
def subscribe(request: Request):
    return build_subscription(resolve_base(request), resolve_sharecode(request))


@app.get("/subscribe/single")
def subscribe_single(request: Request):
    return build_subscription(resolve_base(request), resolve_sharecode(request))[0]


@app.get("/subscribe/generic")
def subscribe_generic(request: Request):
    return build_generic_subscription(resolve_base(request), resolve_sharecode(request))


@app.get("/subscribe/generic/single")
def subscribe_generic_single(request: Request):
    return build_generic_subscription(resolve_base(request), resolve_sharecode(request))[0]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT)
