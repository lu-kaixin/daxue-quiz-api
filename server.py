"""
题库服务（多源 · 大学知识题）

设计原则
    1. 优先外部题库源（GitHub 项目 tikuAdapter 接入的两个公开源），本地库兜底；
    2. 本地库分两份：大学知识题 248 道（C-Eval 筛选 234 道 + 核对过的判断 14 道）、
       高等数学题 78 道（选择 + 判断，计算类入库时由 sympy 复核）；
    3. 不再提供 C-Eval 52 学科全量题库（已下线，数据文件移出服务目录）。

来源优先级
    1  buguake  不挂科题库源   GitHub · tikuAdapter   在线，回传题干 + 答案
    2  icodef   言溪题库       GitHub · tikuAdapter   在线，仅回传答案
    3  local    C-Eval 大学知识题                       本地 248 道，离线可用
    4  gaoshu   本地高数题库                            78 道选择 + 判断，计算题经 sympy 复核
    5  mathsolve 本地符号计算   sympy                    在线源没收录的数学题直接算

数学题一律不走在线源
    两个在线源背后是人文社科网课题库，对高数题实测会给出错答案（微分方程通解
    被答成 y=2x+C、无穷小判断被答成「错」）。数学题只认本地计算结果与高数题库，
    两者都没有就返回未收录——宁可留空，也不填一个错的。

数学题特殊处理
    - 指纹校验：数字序列不一致直接判 0 分，拦住 sin3x/x 与 sinx/x 这类差一个系数的误配；
    - 本地计算：极限/定积分/极值/行列式/求导/偏导/级数敛散/极值拐点/判断题校验，
      算出的值与选项完全一致才返回，算不出或对不上就 404，绝不猜。

启动：
    cd daxue-quiz-api                # 进入仓库目录，server.py 与数据文件同级
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

import base64
import io
import json
import math
import os
import random
import re
import threading
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import MutableHeaders
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

# ---------------- 本地高数题库（可选文件，缺失时服务照常启动） ----------------
GS_DATA = {}
try:
    GS_DATA = json.loads((BASE / "gaoshu.json").read_text(encoding="utf-8"))
except Exception:
    GS_DATA = {}
GS_QUESTIONS = GS_DATA.get("questions") or []
GS_BY_ID = {q["id"]: q for q in GS_QUESTIONS}

PROVIDER = {
    "name": "大学知识题库（多源版）",
    "alias": "《大学启示录：如何读大学？》等大学成长类基础题 + 高等数学选择题/判断题",
    "version": "2.6",
    "description": (
        "多源检索：优先 GitHub 公开题库源，本地保留 {} 道大学知识题"
        "（C-Eval 筛选 234 道 + 补充判断 14 道）与 {} 道高等数学题（选择 + 判断，"
        "计算题经 sympy 复核）；数学题优先本地符号计算，算出的值须与选项完全一致，"
        "且不接受在线源的答案。"
    ).format(len(DX_QUESTIONS), len(GS_QUESTIONS)),
    "author": "主体题目来自 C-Eval 开源数据集（按主题筛选重组），补充判断题经 GitHub tikuAdapter 接入的公开题库核对",
    "homepage": DX_DATA["source"]["site"],
    "dataset": DX_DATA["source"]["dataset"],
    "dataset_url": DX_DATA["source"]["dataset_url"],
    "license": DX_DATA["source"]["license"],
    "license_url": DX_DATA["source"]["license_url"],
    "update_time": "2026-11-28",
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
        "count": len(DX_QUESTIONS) + len(GS_QUESTIONS),
        "note": "从 C-Eval 52 学科中筛出的大学成长主题题，离线可用。",
    },
    {
        "id": "gaoshu",
        "name": "高等数学题库",
        "via": "本地库 · 项目自建",
        "repo": "",
        "priority": 4,
        "online": False,
        "endpoint": "",
        "returns": "题干 + 选项 + 答案 + 解析",
        "count": len(GS_QUESTIONS),
        "note": "高数选择题与判断题，覆盖极限、连续、导数、中值定理、积分、级数、微分方程、多元微分。计算类题目在入库时由 sympy 逐题复核。",
    },
    {
        "id": "mathsolve",
        "name": "本地符号计算",
        "via": "Python · sympy",
        "repo": "https://www.sympy.org",
        "priority": 5,
        "online": False,
        "endpoint": "",
        "returns": "计算结果（含推导）",
        "note": "题库没收录的数学题，本地直接算：极限、定积分、极值、行列式、组合概率、偏导、级数敛散、判断题校验。只在与选项完全一致时才给出答案。",
    },
]
SOURCE_BY_ID = {s["id"]: s for s in SOURCES}
# gaoshu / mathsolve 不是"在线检索源"，是本地库与计算补位，不参与在线检索顺序
DEFAULT_ORDER = [s["id"] for s in sorted(SOURCES, key=lambda x: x["priority"])
                 if s["id"] not in ("gaoshu", "mathsolve")]

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


# ---------------- 数学题：符号规范化 + 严格校验 ----------------
# 数学题不能只按字面相似度判命中：同一个模板换个系数就是另一道题，
# 而「sin3x/x」和「sinx/x」的字面相似度高达 0.75，直接采纳会给出错误答案。
# 所以对数学题加一道硬校验：
#   1) 先把符号统一（×→*、÷→/、−→-，NFKC 顺带把 ²→2、₀→0、（）→()）;
#   2) 抽出「字母词 + 数字 + 运算符」序列作为指纹；
#   3) 两边数字序列必须完全一致，且指纹重合度过半，否则判为不同题。
# 宁可返回 404，也不返回一个算错的答案。
_MATH_MARKS = re.compile(r"[∫∬∭∑∏√≤≥≠≈∞π±→∂∇∈∉⊂⊆∪∩∀∃]")
_MATH_FUNCS = (
    "arcsin", "arccos", "arctan", "sin", "cos", "tan", "cot", "sec", "csc",
    "lim", "log", "ln", "lg", "exp", "det", "max", "min", "sup", "inf", "mod",
)
_MATH_FUNC_RE = re.compile(r"(?<![a-z])(?:%s)(?![a-z])" % "|".join(sorted(_MATH_FUNCS, key=len, reverse=True)))
_MATH_TOK_RE = re.compile(r"[a-z]+|\d|\.|[+\-*/^=<>!|\[\]{}()]|[∫∬∭∑∏√≤≥≠≈∞π∂∇]")
_MATH_SYMBOL_MAP = (
    ("×", "*"), ("✕", "*"), ("✖", "*"), ("·", "*"), ("⋅", "*"), ("∗", "*"),
    ("÷", "/"), ("−", "-"), ("–", "-"), ("—", "-"), ("―", "-"),
)

# 高数文字题的特征词：出现这些词基本可以断定是数学题，必须走本地计算/本地题库，
# 不能交给人文社科在线源（实测「若 f'(x0)=0 则 x0 是极值点」被答成「错」）。
_MATH_CONCEPT_RE = re.compile(
    r"(可导|可微|偏导|全微分|微分方程|原函数|不定积分|定积分|广义积分|变上限积分|"
    r"无穷小|无穷大|级数|收敛半径|拐点|驻点|极值点|极限值|单调递增|单调递减|"
    r"拉格朗日|罗尔定理|洛必达|二重积分|雅可比|特征方程|可积|"
    r"处\s*(?:连续|可导|可微|可积)|函数[^，。]{0,12}连续)"
)


def mathnorm(text: str) -> str:
    """数学表达式归一化：全角/上标/运算符统一，去空白。"""
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    for a, b in _MATH_SYMBOL_MAP:
        s = s.replace(a, b)
    s = re.sub(r"\s+", "", s)
    # ≥ ≤ ≠ 这类符号 NFKC 不动，但中文输入法常给出 >= <= !=，两边统一成 >=
    for a, b in (("≥", ">="), ("≤", "<="), ("≠", "!=")):
        s = s.replace(a, b)
    return s


def is_math(text: str) -> bool:
    """粗判是否数学表达式题。只认强特征，避免把普通文字题当数学题。"""
    s = str(text or "")
    if not s:
        return False
    if _MATH_MARKS.search(s):
        return True
    if re.search(r"[²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉]|\^", s):
        return True
    if _MATH_FUNC_RE.search(s.lower()):
        return True
    if re.search(r"\d\s*[a-zA-Z]\s*[+\-*/^]", s) or re.search(r"\d+\s*[+\-*/^=]\s*\d+", s):
        return True
    # sinx、cos2x、f'(0) 这类写法：函数名后面紧跟变量（没括号），
    # 或者函数名带撇号求值。不认的话「设 f(x)=sinx，则 f'(0)=」会被当成文字题送去在线源。
    if re.search(r"(?<![a-z])(?:sin|cos|tan|cot|ln|lg|arcsin|arctan|e)\s*\d*[a-zA-Z]\b", s.lower()):
        return True
    if re.search(r"[a-zA-Z]\s*['′]\s*\(|[a-zA-Z]\s*['′]\s*\d", s):
        return True
    # 映射类：f:(x,y)→(...) 求原像、|A| 到 |B| 的映射个数
    if re.search(r"(原像|逆像)", s) and re.search(r"[（(]\s*[a-zA-Z]\s*[,，]\s*[a-zA-Z]\s*[)）]", s):
        return True
    if "映射" in s and re.search(r"[A-Z]\s*=\s*\{[^{}]*\}", s):
        return True
    # 高数的文字型题（判断题、概念选择题）：没有公式但明确是数学命题
    if _MATH_CONCEPT_RE.search(s):
        return True
    return False


def math_key(text: str) -> list:
    """数学指纹：去掉题号、空括号、中文，只留字母词/数字/运算符。"""
    s = mathnorm(text)
    s = re.sub(r"^\d+\s*[.、)．]", "", s)
    s = re.sub(r"^第\d+题", "", s)
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"[\u4e00-\u9fa5，。；：？！、]", "", s)
    return _MATH_TOK_RE.findall(s)


def math_guard(query: str, target: str) -> bool:
    """数学题硬校验：数字序列必须完全一致，指纹重合度 ≥ 0.55。"""
    kq, kt = math_key(query), math_key(target)
    if not kq or not kt:
        return True
    dq = [x for x in kq if x[0].isdigit()]
    dt = [x for x in kt if x[0].isdigit()]
    if dq != dt:
        return False
    # 光比数字还不够：sin3x/x 与 (sinx-x)/x^3 的数字都是 [0, 3]，
    # 但变量 x 出现 3 次 vs 4 次，答案一个是 3、一个是 -1/6。
    # 字母词（变量、函数名）的个数对不上，直接判不是同一题。
    # 注意只数字母词：运算符（^ = ( )）不算，否则 x² 与 x^2 这种同题异写会被误杀。
    if len([x for x in kq if x[0].isalpha()]) != len([x for x in kt if x[0].isalpha()]):
        return False
    aq, at = set(kq), set(kt)
    union = len(aq | at)
    return bool(union) and len(aq & at) / union >= 0.55


def math_variants(keyword: str) -> list:
    """数学题的备用查询串：在线题库按词检索，符号是噪声。

    v1 符号统一版（× → *，去掉空格）；
    v2 纯核心版（连运算符、括号一起去掉，只留中文+数字+函数名）。
    """
    if not is_math(keyword):
        return []
    mn = mathnorm(keyword)
    out = []
    if mn and mn != normalize(keyword):
        out.append(mn)
    core = re.sub(r"[+\-*/^=<>!|\[\]{}()~_∫∬∭∑∏√≤≥≠≈∞π∂∇]", " ", mn)
    core = re.sub(r"[\s,，。；]+", " ", core).strip()
    if len(core) >= 6 and core not in out:
        out.append(core)
    return out[:2]


def _meaningful_len(text: str) -> int:
    """有效字符数：汉字、字母、数字。用来判断 OCR 出来的东西够不够定位一道题。"""
    return len(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]", text or ""))


def score(query: str, target: str) -> float:
    """0~1 相似度：包含关系优先，其次字符二元组重合度。

    数学题先过 math_guard：指纹不符直接 0 分，避免「差一个系数」被当成同一题。
    """
    if is_math(query) and is_math(target) and not math_guard(query, target):
        return 0.0
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
def local_search(keyword: str, limit: int = 3, pool: list = None) -> list:
    """本地库检索。pool 为空时只查大学知识题，传 pool 可把高数题库一起查。"""
    scored = []
    for q in (DX_QUESTIONS if pool is None else pool):
        s = score(keyword, q["question"])
        s = max(s, score(keyword, q["explanation"]) * 0.78)
        s = max(s, score(keyword, " ".join(q["options"])) * 0.55)
        if s > 0:
            scored.append((s, q))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


# 两个本地库合并检索：大学知识题 + 高数题库
BANK_QUESTIONS = DX_QUESTIONS + GS_QUESTIONS


def bank_search(keyword: str, limit: int = 3) -> list:
    return local_search(keyword, limit, pool=BANK_QUESTIONS)


# 同一道题干在题库里常有好几个版本（选项顺序不同、选项文字不同），
# 实测「新时期爱国主义的主题是____」有两个答案完全不同的版本。
# 学生页面上给的是哪套选项，就必须按哪套回答，所以候选里挑选项重合度最高的。
def _strip_opt_prefix(x) -> str:
    return re.sub(r"^\s*[A-Za-z]\s*[.、)）:：]\s*", "", str(x or ""))


def _opt_overlap(a: list, b: list) -> float:
    sa = {_optnorm(_strip_opt_prefix(x)) for x in (a or [])}
    sb = {_optnorm(_strip_opt_prefix(x)) for x in (b or [])}
    sa.discard("")
    sb.discard("")
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def pick_bank_candidate(rows: list, opts: list, thresh: float):
    """从本地候选里挑一条：先按阈值筛，再按与页面选项的重合度排序。

    选项完全对不上时（本校卷子和题库不是同一套），不直接放弃——
    只要答案原文能在页面选项里定位到，就仍然可用；定位不到才放弃。
    """
    good = [(s, q) for s, q in rows if s >= thresh]
    if not good:
        return None
    if not opts or len(opts) < 2:
        return good[0] + (1.0,)
    scored = []
    for s, q in good:
        scored.append((_opt_overlap(opts, q.get("options") or []), s, q))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    for ov, s, q in scored:
        if ov >= 0.34:
            return (s, q, ov)
        ans = q["options"][q["answer"]] if q.get("options") else ""
        if ans and len(ans) <= 40 and answer_as_letter(ans, opts):
            return (s, q, ov)
    return None


# 判断题的题干是纯文字，同一句话换个写法（「在某点可导」vs「f(x) 在 x0 处可导」）
# 字面相似度就掉到 0.3 以下，按普通阈值根本命中不了。这里去掉虚词取骨架再比，
# 只在「选项是正确/错误」时启用，且要求骨架高度一致，避免把反向命题当成同一句。
def judge_core(text: str) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    # f'(x) 先保留成标记再删 f(x)：否则「f(x) 可导则 f'(x) 连续」会和
    # 「f(x) 可导则 f(x) 连续」剥成同一个骨架，一真一假撞在一起。
    s = re.sub(r"[fgh]\s*['′]\s*[（(]\s*[xyz]\s*[)）]", "fprime", s)
    s = re.sub(r"[fgh]\s*[（(]\s*[xyz]\s*[)）]", "", s)
    s = re.sub(r"(如果|那么|对于|任意|一定|必然|必|都|若|则|函数|在|处|的|它|该点|某点)", "", s)
    s = _PUNC.sub("", s)
    s = re.sub(r"x\s*0|y\s*0", "", s)
    return s


def judge_bank_search(keyword: str, limit: int = 2) -> list:
    from difflib import SequenceMatcher

    core = judge_core(keyword)
    if len(core) < 4:
        return []
    hits = []
    for q in GS_QUESTIONS + DX_QUESTIONS:
        if q.get("type") not in ("tf", "judge") and not _is_judge_options(
                _parse_option_lines(q.get("options") or [])):
            continue
        r = SequenceMatcher(None, core, judge_core(q["question"])).ratio()
        if r >= 0.88:
            hits.append((r, min(0.99, 0.80 + r * 0.2), q))
    hits.sort(key=lambda x: x[0], reverse=True)
    # 骨架一样但答案相反（一真一假两个命题），说明这种比法分不开，宁可不答
    if len(hits) >= 2 and hits[0][0] == hits[1][0] and hits[0][2].get("answer") != hits[1][2].get("answer"):
        return []
    return [(s, q) for _, s, q in hits[:limit]]


# 选项字母形态：单字母 / 2-6 个大写连写(ABD) / 字母间夹分隔符(A,B,D / A#B)
# 刻意排除 "A book is on the desk" 这类英文句子——字母之间必须有分隔符，
# 单纯空格+单词不算，否则整句会被拆成一串字母。
_LETTERS_RE = re.compile(r"^[A-Za-z]$|^[A-Z]{2,6}$|^[A-Za-z](?:[\s,，#、/|\-]+[A-Za-z])+$")


# 选项字母后必须跟真正的分隔符。刻意不含括号：「f(x)=x²」「f(x)」这种函数表达式
# 一开头就是「字母 + 左括号」，含括号会把整条答案压成字母 F。
_OPT_SEP_RE = re.compile(r"^\s*([A-Za-z])\s*[.、,，:：)）]")


def ocs_answer(answer) -> str:
    """把上游各种答案格式规整成 OCS 能识别的形式。

    OCS 只认三种：单选 "A"、多选 "A#B#D"、判断 "正确"/"错误"。
    上游常见脏格式：
        "A.充分条件;"                      -> "A"
        "A.xxx;B.yyy;"                     -> "A#B"
        "A(belong to 后面接人称代词宾格)。"   -> "A"
    英文句子（"A book is on the desk"）里 A 后面是空格，不在分隔符集合内，
    不会被误当成选项字母。
    """
    a = (answer or "").strip()
    if not a:
        return ""
    if _LETTERS_RE.match(a):
        return "#".join(re.findall(r"[A-Za-z]", a)).upper()
    segs = [s for s in re.split(r"[;；]", a) if s.strip()]
    letters = []
    for s in segs:
        m = _OPT_SEP_RE.match(s)
        if not m:
            break
        letters.append(m.group(1).upper())
    if letters and len(letters) == len(segs):
        return "#".join(dict.fromkeys(letters))
    return a


_URLISH_RE = re.compile(r"(chaoxing|https?://|www\.|\.(?:png|jpe?g|gif|webp|bmp)\b|star3/origin)", re.I)


def answer_usable(text) -> bool:
    """答案是否像个真答案。

    言溪对没收录的题会回一张答案截图（"p.ananas.chaoxing.com/star3/origin/xxx.png"），
    以前会被当成答案是 "P"，填进卷子等于瞎选。这类一律按「没有答案」处理。
    """
    t = str(text or "").strip()
    if not t or len(t) > 60:
        return False
    return not _URLISH_RE.search(t)


def _parse_option_lines(lines: list) -> list:
    """把 OCR 出来的 ["A.4/3","B. 5/3"] 解析成 [("A","4/3"),("B","5/3")]。"""
    out = []
    for ln in lines or []:
        m = re.match(r"^\s*([A-Za-z])\s*[.、)）:：]?\s*(.+)$", str(ln or "").strip())
        if m:
            out.append((m.group(1).upper(), m.group(2).strip()))
    return out


def _optnorm(text: str) -> str:
    """选项文本归一化：只做全角/大小写/标点处理，不删停用词、不删「正确/错误」。

    normalize() 会把「正确」「错误」当停用词剥掉，「批评是正确的」和
    「批评是错误的」就成了同一个串，反查字母时必然选错。
    """
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _PUNC.sub("", s)


def answer_as_letter(answer: str, option_lines: list) -> str:
    """题库常回答案原文（"4/3"、"充分条件"），而 OCS 只认字母。

    用 OCR 到的选项行把答案文本反查回选项字母——数学题尤其常见：
    上游给的是数值，学生界面要的是勾选。
    """
    ans = mathnorm(answer) if is_math(answer) else _optnorm(answer)
    if not ans or len(ans) > 40:
        return ""
    parsed = _parse_option_lines(option_lines)
    if len(parsed) < 2:
        return ""
    # 数对单独比：(1,1) 与 (1,10) 用字符串包含会误判，必须按数值逐个比
    ap = _pair_values(answer)
    if ap:
        for letter, text in parsed:
            got = _pair_values(text)
            if got and abs(got[0] - ap[0]) <= 1e-9 and abs(got[1] - ap[1]) <= 1e-9:
                return letter
    # 先找完全相等的选项。必须分两轮：只按包含关系找的话，
    # 「同阶但不等价无穷小」会把「等价无穷小」当成命中（子串），直接答错。
    texts = []
    for letter, text in parsed:
        t = (mathnorm(text) if is_math(text) else _optnorm(text)).strip("。.．、,，;；:：")
        texts.append((letter, t))
        if t and t == ans:
            return letter
    # 再退回包含关系，且必须只有一个选项命中，否则宁可返回空
    hits = [letter for letter, t in texts if t and len(ans) >= 2 and (ans in t or t in ans)]
    return hits[0] if len(hits) == 1 else ""


_JUDGE_TRUE = {"正确", "对", "是", "真的", "√"}
_JUDGE_FALSE = {"错误", "错", "否", "假的", "×"}


def fix_answer_letter(payload: dict, option_lines: list) -> dict:
    """答案不是字母时，尝试用选项行反查；查不到就保持原样（前端按文本显示）。"""
    if not option_lines:
        return payload
    a = (payload.get("answer_ocs") or "").strip()
    if not a or _LETTERS_RE.match(a):
        return payload
    # 判断题必须保留「正确 / 错误」原文：OCS 对判断题按文本匹配，
    # 换成 A / B 反而填不上。
    if a in ("√", "✓", "✔", "×", "✗", "✘"):
        # 题库常回 √ / ×，但 OCS 判断题的选项是「正确 / 错误」，符号对不上，
        # 得换成选项原文，否则等于没填。
        plain = [re.sub(r"^[A-Za-z]\s*[.、)）:：]\s*", "", str(o)).strip() for o in option_lines[:2]]
        if len(plain) == 2 and plain[0] in _JUDGE_TRUE and plain[1] in _JUDGE_FALSE:
            payload["answer_ocs"] = plain[0] if a in ("√", "✓", "✔") else plain[1]
            payload["judge_symbol"] = a
            return payload
        return payload
    if a in ("正确", "错误", "对", "错"):
        return payload
    letter = answer_as_letter(payload.get("answer") or "", option_lines)
    if letter:
        payload["answer_ocs"] = letter
        payload["answer_letter"] = letter
        payload["answer_text"] = "{} . {}".format(letter, payload.get("answer") or "")
        payload["letter_from_ocr"] = True
    return payload


_OPT_ANSWER_RE = re.compile(r"^([A-H])\s*[.、)）:：]\s*(.+)$")


def remap_answer_by_option_text(payload: dict, option_lines: list, is_math_q: bool = False) -> dict:
    """在线源给的 "B.(1,1)" 里，字母是它自己那份卷子的选项顺序，文本才是稳定的。

    实测同一道原像题：不挂科上有三个版本，答案分别是 "B.(1,1)"、"C.(1,1)"、
    "D.(1,1)" —— 字母各不相同，选项内容一致。所以只要答案带选项文本，
    就拿文本回本页选项里反查字母；查不到时数学题直接判不可信。
    """
    # answer_ocs 已被 ocs_answer() 压成纯字母（"B.(1,1)" → "B"），
    # 选项文本要去原始 answer 里取，否则丢了唯一可靠的比对依据。
    m = _OPT_ANSWER_RE.match((payload.get("answer_ocs") or "").strip())
    if not m:
        m = _OPT_ANSWER_RE.match((payload.get("answer") or "").strip())
    if not m or not option_lines:
        return payload
    text = m.group(2).strip()
    letter = answer_as_letter(text, option_lines)
    if letter:
        if letter != m.group(1):
            payload["letter_remapped_from"] = m.group(1)
        payload["answer_letter"] = letter
        payload["answer_ocs"] = letter
        payload["answer_text"] = "{} . {}".format(letter, text)
        return payload
    if is_math_q:
        # 数学题选项少一个都对不上答案，说明这份选项和题库不是同一套，
        # 照抄字母就是瞎选（实测只剩 A、B 两个选项时被填成 B，正确是 C）。
        payload["_inconsistent"] = True
    return payload


# ---------------- 数学题：本地符号计算（在线源没收录时的补位） ----------------
# 两个在线源背后是网课人文题库，工科数学覆盖率极低（实测 8 次查询 0 命中，
# 命中的还都是「换了个系数」的另一道题）。能解析出表达式的题干脆本地算：
# 极限、定积分、极值、行列式、组合概率——纯 Python，毫秒级，不花钱不联网。
# 规则只有一条：算出结果、且能对上 OCR 到的某个选项，才返回；
# 算得出但选项对不上，说明题目被识别残了，宁可 404 也不猜。

_ENGINE = None
_SUP_MAP = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    "ⁿ": "n", "⁺": "+", "⁻": "-",
    "₀": "_0", "₁": "_1", "₂": "_2", "₃": "_3", "₄": "_4",
    "₅": "_5", "₆": "_6", "₇": "_7", "₈": "_8", "₉": "_9",
}
_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def math_engine():
    global _ENGINE
    if _ENGINE is None:
        try:
            import sympy  # noqa: F401
            _ENGINE = sympy
        except Exception:
            _ENGINE = False
    return _ENGINE or None


def math_engine_available() -> bool:
    return bool(math_engine())


def _desup(text: str) -> str:
    """x² → x^2、∫₀¹ → ∫_0^1。

    必须先做这一步再 NFKC：NFKC 会把 ² 直接变成 2，
    幂次和积分上下限的信息就永久丢了。
    """
    out = []
    for ch in str(text or ""):
        v = _SUP_MAP.get(ch)
        if v is None:
            out.append(ch)
            continue
        out.append(v if v.startswith("_") else "^" + v)
    return "".join(out)


def _expr(text):
    """把一段表达式转成 sympy 对象，失败返回 None。"""
    sp = math_engine()
    if not sp or not text:
        return None
    from sympy.parsing.sympy_parser import (
        convert_xor, implicit_multiplication_application, parse_expr, standard_transformations,
    )

    # 顺序不能反：先还原上标/下标（x³ → x^3、∫₀¹ → ∫_0^1），
    # 再 NFKC——NFKC 会把 ³ 直接压成 3，幂次信息就没了。
    t = unicodedata.normalize("NFKC", _desup(str(text)))
    t = t.replace("−", "-").replace("×", "*").replace("÷", "/").replace("·", "*").replace("⋅", "*")
    t = t.replace("∞", "oo").replace("π", "pi").replace("√", "sqrt")
    t = re.sub(r"[\u4e00-\u9fa5，。；：？！、]", "", t)
    # sin3x / ln x 这类省略括号的写法，sympy 会拆成 s*i*n*3*x，必须补括号
    t = re.sub(
        r"(arcsin|arccos|arctan|sin|cos|tan|cot|sec|csc|ln|exp|log)(\s*)(\d*\.?\d*)([a-zA-Z])(\^\d+)?",
        lambda m: "{}({}{}{})".format(m.group(1), m.group(3), m.group(4), m.group(5) or ""),
        t,
    )
    # √n 换成 sqrt 后必须补括号：不补会被隐式乘法拆成 sqrt*n（解析出 n*q*r*t/s 这种垃圾）
    t = re.sub(r"sqrt\s*([a-zA-Z]\w*|\d+(?:\.\d+)?)", r"sqrt(\1)", t)
    t = re.sub(r"\s+", "", t)
    t = t.strip("=＝.")
    if not t or len(t) > 120:
        return None
    try:
        return parse_expr(
            t,
            local_dict={"e": sp.E, "pi": sp.pi, "oo": sp.oo},
            transformations=standard_transformations + (implicit_multiplication_application, convert_xor),
        )
    except Exception:
        return None


def _to_num(text):
    """选项文本 → 有限数值（4/3、-2、25/28 都认；∞ 一律不认）。

    选项常写成 "3^4=81" 这种「式子=结果」，直接解析会失败：
    两边都能解析且相等时取右边，这样 81 才能对上。
    """
    # ln2 / ln3 这种省略括号的写法 sympy 会拆成 ln*2，这里直接按自然对数算
    m = re.fullmatch(r"\s*ln\s*(\d+(?:\.\d+)?)\s*", str(text or ""))
    if m:
        return math.log(float(m.group(1)))
    e = _expr(text)
    if e is None and "=" in str(text or ""):
        left, _, right = str(text).partition("=")
        a, b = _expr(left), _expr(right)
        if a is not None and b is not None:
            try:
                if math.isfinite(float(a)) and abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(b))):
                    e = b
            except Exception:
                e = None
    if e is None:
        return None
    try:
        v = float(e)
    except Exception:
        return None
    return v if math.isfinite(v) else None


def _pick_option(value, parsed_options) -> tuple:
    """把计算结果对到选项上，返回 (letter, option_text)；对不上返回 ("", "")。"""
    if value is None:
        return "", ""
    try:
        v = float(value)
    except Exception:
        return "", ""
    if not math.isfinite(v):  # NaN / ∞
        return "", ""
    for letter, text in parsed_options:
        num = _to_num(text)
        if num is None:
            continue
        if abs(num - v) <= 1e-9 * max(1.0, abs(num)):
            return letter, text
    return "", ""


def _pair_values(text):
    """从 "(1,3)" / "（1/2，1/2）" 取出数对；取不到返回 None。"""
    m = re.search(r"[(（]\s*([^,，()（）\s]+)\s*[,，]\s*([^,，()（）\s]+)\s*[)）]", str(text or ""))
    if not m:
        return None
    a, b = _to_num(m.group(1)), _to_num(m.group(2))
    if a is None or b is None:
        return None
    return (a, b)


def _pick_pair(pair, parsed_options) -> tuple:
    """把算出的点 (x, y) 对到选项上。"""
    if not pair or len(pair) != 2:
        return "", ""
    for letter, text in parsed_options:
        got = _pair_values(text)
        if not got:
            continue
        if abs(got[0] - pair[0]) <= 1e-9 * max(1.0, abs(pair[0])) and \
                abs(got[1] - pair[1]) <= 1e-9 * max(1.0, abs(pair[1])):
            return letter, text
    return "", ""


def _pick_count(value, parsed_options) -> tuple:
    """计数题的选项常写成 "3⁴=81"：直接比数值比不了，退一步比「选项里最后一个数」。

    只认唯一命中：两个选项都含这个数说明题干或选项被识别残了，交回 404。
    非整数结果直接不进来——否则 0.693（ln2）会被 round 成 1，错配到选项「1」。
    """
    try:
        raw = float(value)
        v = int(round(raw))
    except Exception:
        return "", ""
    if not math.isfinite(raw) or abs(raw - v) > 1e-9:
        return "", ""
    hits = []
    for letter, text in parsed_options:
        nums = re.findall(r"\d+", str(text or ""))
        if nums and int(nums[-1]) == v:
            hits.append((letter, text))
        elif nums and int(nums[0]) == v and len(nums) == 1:
            hits.append((letter, text))
    return hits[0] if len(hits) == 1 else ("", "")


def _pick_expr(value, parsed_options) -> tuple:
    """符号结果（2*x*y 这种）对选项：两边相减化简为 0 才算命中，且必须唯一。"""
    sp = math_engine()
    if not sp:
        return "", ""
    hits = []
    for letter, text in parsed_options:
        e = _expr(text)
        if e is None:
            continue
        try:
            if sp.simplify(e - value) == 0:
                hits.append((letter, text))
        except Exception:
            continue
    return hits[0] if len(hits) == 1 else ("", "")


def _match_value(value, parsed_options) -> tuple:
    """按结果类型选匹配方式：数对走 _pick_pair，符号式走 _pick_expr，
    数值先精确比、再按「选项最后一个数」兜底。"""
    if isinstance(value, (tuple, list)):
        return _pick_pair(value, parsed_options)
    if getattr(value, "free_symbols", None):
        return _pick_expr(value, parsed_options)
    letter, text = _pick_option(value, parsed_options)
    if letter:
        return letter, text
    return _pick_count(value, parsed_options)


def _detect_kind(text: str) -> str:
    if re.search(r"(原像|逆像)", text):
        return "preimage"
    if "映射" in text and re.search(r"\{\s*[^{}]*\}", text) and re.search(r"(有|共)\s*[(（]\s*[)）]?\s*个|多少个|几个", text):
        return "mapcount"
    if re.search(r"lim\s*[\(（]|极限", text) or re.search(r"lim\s*[a-zA-Z]\s*(?:→|->)", text):
        return "limit"
    if "∫" in text or "积分" in text:
        return "integral"
    if re.search(r"∂\s*[a-zA-Z]\s*/\s*∂", text):
        return "partial"
    if re.search(r"\|\s*[A-Za-z]\s*\||行列式", text):
        return "det"
    if re.search(r"(极小|极大|最小值|最大值)", text):
        return "extrema"
    if re.search(r"(导数|切线|导数值|dy\s*/\s*dx)", text):
        return "deriv"
    if "概率" in text and re.search(r"\d+\s*个", text):
        return "prob"
    return ""


def _solve_limit(text: str):
    sp = math_engine()
    m = re.search(r"lim\s*[\(（]?\s*([a-zA-Z])\s*(?:→|->|－>|—>)\s*([^\)）\s]+)\s*[\)）]?\s*(.+)", text)
    if not m:
        return None, ""
    var, point, body = m.group(1), m.group(2), m.group(3)
    body = re.split(r"[=＝]", body)[0]
    body = re.sub(r"[\(（]\s*[\)）]", "", body)
    e = _expr(body)
    if e is None:
        return None, ""
    try:
        return float(sp.limit(e, sp.Symbol(var), _expr(point))), "limit"
    except Exception:
        return None, ""


def _solve_integral(text: str):
    sp = math_engine()
    m = re.search(r"∫\s*_([^^\(\)\s]+)\s*\^\s*([^^\(\)\s]+)\s*(.+?)\s*d\s*([a-zA-Z])", text)
    if not m:
        return None, ""
    lo, hi, body, var = m.group(1), m.group(2), m.group(3), m.group(4)
    e = _expr(body)
    if e is None:
        return None, ""
    try:
        return float(sp.integrate(e, (sp.Symbol(var), _expr(lo), _expr(hi)))), "integral"
    except Exception:
        return None, ""


def _solve_det(text: str):
    sp = math_engine()
    rows = re.findall(r"\[([^\[\]]+)\]", text)
    if len(rows) < 2:
        # 教材写法 |1 2; 3 4|、|1 2 3; 4 5 6; 7 8 9|，也接受纯空格分隔
        m = re.search(r"\|\s*([^|]+?)\s*\|", text)
        if not m:
            return None, ""
        body = m.group(1)
        rows = [r for r in re.split(r"[;；]", body) if r.strip()]
        if len(rows) < 2:
            cells = [c for c in re.split(r"[\s,，]+", body) if c]
            if len(cells) in (4, 9):
                k = int(round(len(cells) ** 0.5))
                rows = [" ".join(cells[i * k:(i + 1) * k]) for i in range(k)]
    try:
        mat = [[_expr(c) for c in re.split(r"[,，\s]+", r.strip()) if c] for r in rows]
        if any(v is None for row in mat for v in row):
            return None, ""
        if len({len(r) for r in mat}) != 1:
            return None, ""
        return sp.Matrix(mat).det(), "det"
    except Exception:
        return None, ""


def _solve_extrema(text: str):
    sp = math_engine()
    want_min = bool(re.search(r"(极小|最小值)", text))
    m = re.search(r"(?:[yY]|[fF]\s*\(\s*[a-zA-Z]\s*\))\s*[=＝]\s*([^，,。；;（()）\u4e00-\u9fa5]+)", text)
    if not m:
        return None, ""
    e = _expr(m.group(1))
    if e is None:
        return None, ""
    x = sp.Symbol("x")
    try:
        crit = sp.solve(sp.diff(e, x), x)
    except Exception:
        return None, ""
    cands = []
    for c in crit:
        if not getattr(c, "is_real", True):
            continue
        try:
            d2 = sp.diff(e, x, 2).subs(x, c)
            if want_min and d2 <= 0:
                continue
            if not want_min and d2 >= 0:
                continue
            cands.append(float(e.subs(x, c)))
        except Exception:
            continue
    if not cands:
        return None, ""
    return (min(cands) if want_min else max(cands)), "extrema"


def _solve_deriv(text: str):
    """求 f'(x0)：题干里要求导、且能定位到自变量的取值。"""
    sp = math_engine()
    if not re.search(r"(导数|切线|导数值|f\s*['′]|dy\s*/\s*dx)", text):
        return None, ""
    m = re.search(r"(?:[yY]|[fF]\s*\(\s*[a-zA-Z]\s*\))\s*[=＝]\s*([^，,。；;（()）\u4e00-\u9fa5]+)", text)
    if not m:
        return None, ""
    e = _expr(m.group(1))
    if e is None:
        return None, ""
    mp = re.search(r"[xX]\s*[=＝]\s*(-?\d+(?:\.\d+)?)", text) or re.search(
        r"[fF]\s*['′]\s*\(\s*(-?\d+(?:\.\d+)?)\s*\)", text)
    if not mp:
        return None, ""
    x = sp.Symbol("x")
    try:
        return float(sp.diff(e, x).subs(x, _expr(mp.group(1)))), "deriv"
    except Exception:
        return None, ""


def _solve_prob(text: str):
    sp = math_engine()
    pairs = re.findall(r"(\d+)\s*个\s*([白黑红黄蓝绿])", text)
    m = re.search(r"任取\s*(\d+)\s*个", text)
    if len(pairs) < 2 or not m:
        return None, ""
    n = int(m.group(1))
    a_cnt, a_color = int(pairs[0][0]), pairs[0][1]
    b_cnt, b_color = int(pairs[1][0]), pairs[1][1]
    total = a_cnt + b_cnt
    if n <= 0 or n > total:
        return None, ""

    def _k(pattern):
        mk = re.search(pattern, text)
        if not mk:
            return None
        g = mk.group(1)
        return int(g) if g.isdigit() else _CN_NUM.get(g)

    try:
        allc = sp.binomial(total, n)
        if "至少" in text:
            k = min(_k(r"至少.{0,4}?([一两二三四五六七八九十]|\d+)\s*个?\s*" + a_color) or 1, n)
            bad = sum(sp.binomial(a_cnt, i) * sp.binomial(b_cnt, n - i) for i in range(k))
            return float(1 - sp.Rational(bad, allc)), "prob"
        if "至多" in text:
            k = min(_k(r"至多.{0,4}?([一两二三四五六七八九十]|\d+)\s*个?\s*" + a_color) or 1, a_cnt)
            good = sum(sp.binomial(a_cnt, i) * sp.binomial(b_cnt, n - i) for i in range(k + 1))
            return float(sp.Rational(good, allc)), "prob"
        k = _k(r"恰好.{0,4}?([一两二三四五六七八九十]|\d+)\s*个?\s*" + a_color)
        if k and k <= a_cnt and n - k <= b_cnt:
            return float(sp.binomial(a_cnt, k) * sp.binomial(b_cnt, n - k) / allc), "prob"
        if re.search(r"(都|全)是\s*" + a_color, text) and n <= a_cnt:
            return float(sp.binomial(a_cnt, n) / allc), "prob"
    except Exception:
        return None, ""
    return None, ""


def _solve_preimage(text: str):
    """线性映射求原像：f:(x,y)→(x+2y, 2x-y)，(3,1) 的原像。

    直接解二元一次方程组，不需要「猜」哪条链路；
    解出来是点，用 _pick_pair 去选项里找同一点。
    """
    sp = math_engine()
    m = re.search(
        r"[fF]\s*[:：]?\s*[(（]\s*([a-zA-Z])\s*[,，]\s*([a-zA-Z])\s*[)）]\s*"
        r"(?:→|->|—>|－>|=>)\s*[(（]\s*([^()（）]+?)\s*[,，]\s*([^()（）]+?)\s*[)）]",
        text,
    )
    if not m:
        return None, ""
    u, v = m.group(1), m.group(2)
    e1, e2 = _expr(m.group(3)), _expr(m.group(4))
    if e1 is None or e2 is None:
        return None, ""
    idx = min([i for i in (text.find("原像"), text.find("逆像")) if i >= 0] or [-1])
    head = text[:idx] if idx > 0 else text
    target = None
    for mm in re.finditer(r"[(（]\s*(-?\d+(?:\.\d+)?(?:/\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?(?:/\d+)?)\s*[)）]", head):
        target = mm          # 取「原像」之前最后一个纯数字坐标
    if not target:
        return None, ""
    a, b = _to_num(target.group(1)), _to_num(target.group(2))
    if a is None or b is None:
        return None, ""
    X, Y = sp.Symbol(u), sp.Symbol(v)
    try:
        sol = sp.solve([e1 - a, e2 - b], [X, Y], dict=True)
    except Exception:
        return None, ""
    if not sol:
        return None, ""
    d = sol[0]
    try:
        return (float(d[X]), float(d[Y])), "preimage"
    except Exception:
        return None, ""


def _solve_mapcount(text: str):
    """A 到 B 的映射个数 = |B|^|A|。

    只处理「映射」本身：单射 / 满射 / 双射的计数公式不同，交给各自分支，
    这里见到这些词直接放弃，避免用错公式。
    """
    if re.search(r"(单射|满射|双射|一一映射|一一对应)", text):
        return None, ""
    sets = re.findall(r"([A-Z])\s*=\s*\{([^{}]*)\}", text)
    if len(sets) < 2:
        return None, ""
    got = {}
    for name, body in sets:
        if re.search(r"[|｜:]", body):        # {x|x≥0} 这种描述式集合不是有限集
            return None, ""
        items = [x.strip() for x in re.split(r"[,，]", body) if x.strip()]
        if not items or len(items) > 30:
            return None, ""
        got[name] = items
    if "A" not in got or "B" not in got:
        return None, ""
    return len(got["B"]) ** len(got["A"]), "mapcount"


def _solve_partial(text: str):
    """偏导数：z=x²y，求 ∂z/∂x。符号结果交给 _pick_expr 比对选项。"""
    sp = math_engine()
    m = re.search(r"∂\s*([a-zA-Z])\s*/\s*∂\s*([a-zA-Z])", text)
    if not m:
        return None, ""
    dep, ind = m.group(1), m.group(2)
    mz = re.search(re.escape(dep) + r"\s*[=＝]\s*([^，,。；;（()）\u4e00-\u9fa5]+)", text)
    if not mz:
        return None, ""
    e = _expr(mz.group(1))
    if e is None:
        return None, ""
    try:
        return sp.diff(e, sp.Symbol(ind)), "partial"
    except Exception:
        return None, ""


def _series_term(text: str):
    t = re.sub(r"^\s*[A-Ha-h]\s*[.、)）:：]\s*", "", str(text or ""))
    t = t.replace("∑", "").replace("Σ", "").replace("∞", "")
    return _expr(t.strip(" =＝"))


def _series_converges(term):
    """∑_{n=1}^∞ term 是否收敛：True/False，判不了返回 None。

    不能只靠 sympy 求和：∑1/√n 会被算成 zeta(1/2)（一个有限实数），
    按「结果有限就是收敛」判会直接答错。所以先走 p-判别，再用积分判别兜底。
    """
    sp = math_engine()
    if not sp or term is None:
        return None
    ns = sp.Symbol("n")
    # p-判别：形如 c/n^p，p>1 收敛
    try:
        pw = term.as_powers_dict()
    except Exception:
        pw = {}
    if len(pw) == 1 and ns in pw:
        try:
            p = float(-pw[ns])
            return p > 1
        except Exception:
            return None
    alt = "(-1)**n" in str(term) or "(-1)**(n" in str(term)
    # 积分判别：正项且单调时，∑ 与 ∫ 同敛散
    if not alt:
        try:
            it = sp.integrate(term, (ns, 1, sp.oo))
            if it in (sp.oo, -sp.oo, sp.zoo):
                return False
            if it.is_finite:
                return True
            if it.is_finite is False:
                return False
        except Exception:
            pass
    try:
        s = sp.summation(term, (ns, 1, sp.oo))
    except Exception:
        return None
    if s is None or s.has(sp.Sum):
        return None
    if s in (sp.oo, -sp.oo, sp.zoo, sp.nan):
        return False
    try:
        f = s.is_finite
    except Exception:
        return None
    return None if f is None else bool(f)


def _series_letter(text: str, parsed: list) -> str:
    """「下列级数中收敛的是」：逐个选项算敛散，只有唯一命中才回答。"""
    if "级数" not in text or not re.search(r"(下列|以下|哪个|哪项)", text):
        return ""
    want_conv = "收敛" in text and "发散" not in text
    want_div = "发散" in text
    if not (want_conv or want_div):
        return ""
    verdicts = []
    for letter, opt in parsed:
        v = _series_converges(_series_term(opt))
        if v is None:
            return ""
        verdicts.append((letter, v))
    hits = [l for l, v in verdicts if (v and want_conv) or (not v and want_div)]
    return hits[0] if len(hits) == 1 else ""


def _abs_expr(text: str):
    """把 |x| 转成 Abs(x) 再解析，用于可导/连续性判断。"""
    s = re.sub(r"\|([^|]+)\|", r"Abs(\1)", str(text or ""))
    return _expr(s)


def _point_letter(text: str, parsed: list) -> str:
    """「y=x³ 在 x=0 处」问极大/极小/拐点：靠一阶二阶导数变号判断。"""
    sp = math_engine()
    texts = " ".join(t for _, t in parsed)
    if not re.search(r"(极大|极小|拐点)", texts):
        return ""
    m = re.search(r"[yY]\s*[=＝]\s*([^，,。；;]+?)\s*在\s*x\s*[=＝]\s*(-?\d+(?:\.\d+)?)\s*处", text)
    if not m:
        return ""
    f = _abs_expr(m.group(1))
    if f is None:
        return ""
    x0 = float(m.group(2))
    x = sp.Symbol("x")
    try:
        d1 = sp.diff(f, x)
        d2 = sp.diff(f, x, 2)
        eps = 1e-6

        def sgn(e, pt):
            v = float(e.subs(x, pt))
            return (v > 0) - (v < 0)

        l1, r1 = sgn(d1, x0 - eps), sgn(d1, x0 + eps)
        l2, r2 = sgn(d2, x0 - eps), sgn(d2, x0 + eps)
    except Exception:
        return ""
    if l1 * r1 < 0:                       # 一阶变号 → 极值
        kw = "极小" if l1 < 0 < r1 else "极大"
        hits = [l for l, t in parsed if kw in t]
        return hits[0] if len(hits) == 1 else ""
    if l2 * r2 < 0:                       # 二阶变号 → 拐点
        hits = [l for l, t in parsed if "拐点" in t]
        return hits[0] if len(hits) == 1 else ""
    return ""


_JUDGE_SET = ({"正确", "对", "√", "是"}, {"错误", "错", "×", "否"})


def _is_judge_options(parsed: list) -> bool:
    if len(parsed) != 2:
        return False
    a = re.sub(r"^[A-Za-z]\s*[.、)）:：]\s*", "", parsed[0][1]).strip("。. ")
    b = re.sub(r"^[A-Za-z]\s*[.、)）:：]\s*", "", parsed[1][1]).strip("。. ")
    return a in _JUDGE_SET[0] and b in _JUDGE_SET[1]


def _judge_truth(text: str):
    """判断题命题校验：只判「能算出来」的具体命题。返回 True/False/None。

    覆盖四类：定积分等式、极限等式、某点可导/连续、具体级数敛散。
    定理型命题（若 f'(x0)=0 则…）不做——它要讲逻辑，符号计算给不出结论。
    """
    sp = math_engine()
    if not sp:
        return None
    t = unicodedata.normalize("NFKC", _desup(str(text or "")))

    # 1) 定积分等式：∫_a^b f dx = V
    m = re.search(
        r"∫\s*_\s*[（(]?\s*([^^()（）\s]+?)\s*[)）]?\s*\^\s*[（(]?\s*([^^()（）\s]+?)\s*[)）]?\s*"
        r"(.+?)\s*d\s*([a-zA-Z])\s*[=＝]\s*(.+?)\s*$", t)
    if m:
        lo, hi, body, var, rhs = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        v = _to_num(rhs)
        if v is None:
            return None
        e = _expr(body)
        if e is None:
            return None
        sym = sp.Symbol(var)
        # 积分区间内有瑕点时必须按广义积分算：直接 integrate 可能给出两个发散
        # 部分相消的假结果（∫_{-1}^{1} 1/x 在 sympy 里会被算成 0）。
        try:
            sing = sp.singularities(e, sym)
        except Exception:
            sing = set()
        inner = []
        lo_v, hi_v = _to_num(lo), _to_num(hi)
        for s0 in sing:
            try:
                sv = float(s0)
            except Exception:
                continue
            if lo_v is not None and hi_v is not None and lo_v < sv < hi_v:
                inner.append(sv)
        try:
            if inner:
                pts = [lo_v] + inner + [hi_v]
                pts.sort()
                total = 0.0
                for i in range(len(pts) - 1):
                    seg = sp.integrate(e, (sym, pts[i], pts[i + 1]))
                    fv = float(seg)
                    if not math.isfinite(fv):
                        return False
                    total += fv
                return abs(total - v) <= 1e-9 * max(1.0, abs(v))
            if lo_v is None or hi_v is None:
                # 上下限含字母参数（∫_{-a}^{a}）时算不出具体值，交给题库，不要瞎判
                return None
            fv = float(sp.integrate(e, (sym, lo_v, hi_v)))
        except Exception:
            return None
        if not math.isfinite(fv):
            return False
        return abs(fv - v) <= 1e-9 * max(1.0, abs(v))

    # 5) 广义积分敛散：∫_0^1 1/x² dx 发散
    m = re.search(
        r"∫\s*_\s*[（(]?\s*([^^()（）\s]+?)\s*[)）]?\s*\^\s*[（(]?\s*([^^()（）\s]+?)\s*[)）]?\s*"
        r"(.+?)\s*d\s*([a-zA-Z])\s*(收敛|发散)", t)
    if m:
        lo, hi, body, var, word = m.groups()
        e = _expr(body)
        lo_v, hi_v = _to_num(lo), _to_num(hi)
        if e is None or lo_v is None or hi_v is None:
            return None
        sym = sp.Symbol(var)
        try:
            sing = sp.singularities(e, sym)
        except Exception:
            sing = set()
        if any(lo_v < float(s0) < hi_v for s0 in sing if getattr(s0, "is_real", False)):
            converged = False           # 区间内部有瑕点，按发散处理（不取主值）
        else:
            try:
                res = sp.integrate(e, (sym, lo_v, hi_v))
                converged = bool(res.is_finite)
            except Exception:
                converged = False
        return converged if word == "收敛" else (not converged)

    # 2) 极限等式：lim(x→a) expr = V
    m = re.search(r"lim\s*[（(]?\s*([a-zA-Z])\s*(?:→|->)\s*([^)）\s]+)\s*[)）]?\s*(.+?)\s*[=＝]\s*(-?[\d.]+(?:/\d+)?|∞|e)\s*$", t)
    if m:
        var, point, body, rhs = m.group(1), m.group(2), m.group(3), m.group(4)
        e = _expr(body)
        if e is None:
            return None
        try:
            got = sp.limit(e, sp.Symbol(var), _expr(point))
        except Exception:
            return None
        if got in (sp.oo, -sp.oo, sp.zoo, sp.nan) or not got.is_finite:
            return False
        v = _to_num(rhs)
        if v is None:
            return None
        return abs(float(got) - v) <= 1e-9 * max(1.0, abs(v))

    # 3) 某点可导 / 连续：函数 y=|x| 在 x=0 处可导
    m = re.search(r"(?:函数\s*)?[yYfF]\s*(?:\(\s*[a-zA-Z]\s*\))?\s*[=＝]\s*(.+?)\s*在\s*x\s*[=＝]\s*"
                  r"(-?\d+(?:\.\d+)?)\s*处\s*(可导|连续|可微)", t)
    if m:
        f = _abs_expr(m.group(1))
        if f is None:
            return None
        x0 = float(m.group(2))
        sym = sp.Symbol("x")
        try:
            left = sp.limit(f, sym, x0, "-")
            right = sp.limit(f, sym, x0, "+")
        except Exception:
            return None
        if left in (sp.zoo, sp.oo, -sp.oo) or right in (sp.zoo, sp.oo, -sp.oo):
            return False
        if sp.simplify(left - right) != 0:
            return False                      # 左右极限不等 → 不连续
        if m.group(3) == "连续":
            return True
        try:                                  # 导数：左右差商极限相等才可导
            dl = sp.limit((f - left) / (sym - x0), sym, x0, "-")
            dr = sp.limit((f - left) / (sym - x0), sym, x0, "+")
            return sp.simplify(dl - dr) == 0
        except Exception:
            return False

    # 4) 具体级数：级数 ∑1/n² 收敛
    m = re.search(r"级数\s*(?:∑|Σ)\s*(.+?)\s*(收敛|发散)", t)
    if m:
        conv = _series_converges(_expr(m.group(1)))
        if conv is None:
            return None
        return conv if m.group(2) == "收敛" else (not conv)
    return None


def _judge_letter(text: str, parsed: list) -> str:
    if not _is_judge_options(parsed):
        return ""
    v = _judge_truth(text)
    if v is None:
        return ""
    a, b = parsed[0][0], parsed[1][0]
    return a if v else b


def solve_math(stem: str, option_lines: list) -> dict:
    """尝试本地算出答案。返回 {"letter","answer","method","value"}，算不出返回 {}。"""
    if not math_engine_available():
        return {}
    parsed = _parse_option_lines(option_lines)
    if len(parsed) < 2:
        return {}
    text = unicodedata.normalize("NFKC", _desup(str(stem or "")))
    if not text:
        return {}
    # 必须看选项才能判的题：判断题、级数敛散题、极值/拐点题
    for method, fn in (("judge", _judge_letter), ("series", _series_letter), ("point", _point_letter)):
        try:
            letter = fn(text, parsed)
        except Exception:
            letter = ""
        if letter:
            opt_text = dict(parsed).get(letter, "")
            return {
                "letter": letter,
                "answer": opt_text,
                # 判断题 OCS 按选项原文匹配，返回 "正确"/"错误" 才能填上，返回 A/B 会被忽略
                "ocs": opt_text if method == "judge" else "",
                "value": opt_text,
                "method": method,
            }
    kind = _detect_kind(text)
    solvers = {
        "limit": _solve_limit,
        "integral": _solve_integral,
        "det": _solve_det,
        "extrema": _solve_extrema,
        "prob": _solve_prob,
        "deriv": _solve_deriv,
        "preimage": _solve_preimage,
        "mapcount": _solve_mapcount,
        "partial": _solve_partial,
    }
    order = [kind] if kind in solvers else []
    order += [k for k in ("limit", "integral", "det", "partial", "deriv", "extrema", "prob",
                          "preimage", "mapcount") if k not in order]
    for k in order:
        try:
            value, method = solvers[k](text)
        except Exception:
            continue
        if value is None:
            continue
        letter, opt_text = _match_value(value, parsed)
        if letter:
            if isinstance(value, (tuple, list)):
                shown = "({}, {})".format(*[round(float(x), 6) for x in value])
            elif getattr(value, "free_symbols", None):
                shown = str(value)          # 偏导数这类符号结果
            elif isinstance(value, int):
                shown = int(value)
            else:
                shown = round(float(value), 6)
            return {
                "letter": letter,
                "answer": opt_text,
                "value": shown,
                "method": method,
            }
    return {}


_METHOD_CN = {
    "limit": "极限", "integral": "定积分", "det": "行列式", "extrema": "极值",
    "prob": "组合计数", "deriv": "求导", "preimage": "解方程组求原像", "mapcount": "映射计数",
    "partial": "偏导数", "judge": "命题校验", "series": "级数敛散判别", "point": "极值与拐点判别",
}


def math_payload(keyword: str, sol: dict, option_lines: list) -> dict:
    """本地算出来的结果，包装成和在线源一致的响应结构。"""
    return {
        "id": "mathsolve-{}".format(abs(hash((keyword, sol["letter"]))) % 10 ** 8),
        "question": keyword,
        "answer": sol["answer"],
        "answer_flat": sol["answer"],
        "answer_letter": sol["letter"],
        "answer_text": "{} . {}".format(sol["letter"], sol["answer"]),
        "answer_ocs": sol.get("ocs") or sol["letter"],
        "options": [t for _, t in _parse_option_lines(option_lines)],
        "type": "choice",
        "source": "本地符号计算（sympy）",
        "source_id": "mathsolve",
        "verified": False,
        "explanation": "服务端按{}直接算出 {}，与选项 {} 完全一致。这条链路不查第三方题库、不调付费模型，只做符号运算；"
                       "计算结果对不上任何选项时本服务会返回未收录，不会给近似答案。".format(
                           _METHOD_CN.get(sol["method"], "公式"), sol["value"], sol["letter"]),
    }


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
        "source_id": q.get("source_id", "local"),
        "explanation": q.get("explanation") or "C-Eval 仅在 dev 划分提供官方解析，本题所属划分未提供。",
    }


# ---------------- 源 1：不挂科 ----------------
_UP_MASK = "34cab29ef956d78afd"
_cache: dict = {}


def _up_decrypt(token: str, actk: str) -> str:
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
        if not answer_usable(answer):
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
        if not answer_usable(text):
            continue
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
    if source_id == "gaoshu":
        return [{"question": q["question"], "score": round(s, 4), "_q": q}
                for s, q in local_search(keyword, 5, pool=GS_QUESTIONS)]
    return []


def to_payload(row: dict, source_id: str) -> dict:
    if source_id in ("local", "gaoshu"):
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


def multi_search(keyword: str, count: int = 1, order=None, variants=None):
    """按优先级依次检索，返回 (payloads, trace, used_source)。

    variants：同一道题的不同写法（数学题的符号统一版、核心词版）。
    在线题库按词检索，公式符号是噪声，多给几种写法能明显提高召回；
    每个源依次试这些写法，命中即停，命不中再换下一个源。
    """
    order = order or DEFAULT_ORDER
    kws = [keyword]
    for v in (math_variants(keyword) if variants is None else variants):
        v = (v or "").strip()
        if v and v not in kws:
            kws.append(v)

    trace = []
    for sid in order:
        if sid not in SOURCE_BY_ID:
            continue
        # 数学题的命中门槛单独抬高：差一个系数就是另一道题，不能按普通题放宽。
        threshold = 0.9 if sid == "icodef" else (0.8 if is_math(keyword) else 0.72)
        used_kw, rows = keyword, []
        for kw in kws:
            got = search_source(sid, kw)
            if got and (not rows or got[0]["score"] > rows[0]["score"]):
                rows, used_kw = got, kw
            if rows and rows[0]["score"] >= threshold:
                break
        top = rows[0]["score"] if rows else 0.0
        ok = bool(rows) and top >= threshold
        item = {
            "source": sid,
            "name": SOURCE_BY_ID[sid]["name"],
            "hits": len(rows),
            "top": round(top, 3),
            "used": ok,
        }
        if len(kws) > 1:
            item["keyword_used"] = used_kw
        trace.append(item)
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


class PrivateNetworkMiddleware:
    """Chrome 的 Private Network Access 兜底。

    https 网课页面去请求 http://127.0.0.1 属于「公网页面访问私有网络」，
    新版 Chrome / Edge 会在预检时要求响应带 Access-Control-Allow-Private-Network，
    否则请求直接被拦掉，OCS 里看到的就是「连接失败」。
    油猴 / 脚本猫走 GM_xmlhttpRequest 不受这个限制，但用 fetch 的配置会中招。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Access-Control-Allow-Private-Network"] = "true"
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(PrivateNetworkMiddleware)


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


# ---------------- 图片题：OCR 兜底 ----------------
# 图片题在 OCS 5.x 里表现为：题干中的图片被替换成 [图片1] [图片2] 占位符，
# 真实图片以 base64 数组放在 env.images（由订阅配置的 handler 取出来发给我们）。
# 这里把图片识别成文字，再拼回题干走正常检索。
# rapidocr 是可选依赖：没装就返回 ocr-unavailable，纯文字题完全不受影响。

IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MAX_COUNT = 3
OCR_MIN_SCORE = 0.55

_ocr_engine = None
_ocr_failed = False
_ocr_lock = threading.Lock()


def _get_ocr():
    global _ocr_engine, _ocr_failed
    if _ocr_engine is not None:
        return _ocr_engine
    if _ocr_failed:
        return None
    with _ocr_lock:
        if _ocr_engine is None and not _ocr_failed:
            try:
                from rapidocr_onnxruntime import RapidOCR

                _ocr_engine = RapidOCR()
            except Exception:
                _ocr_failed = True
    return _ocr_engine


def ocr_available() -> bool:
    """只探测依赖是否装了，不触发模型加载（加载约 1 秒）。"""
    if _ocr_engine is not None:
        return True
    if _ocr_failed:
        return False
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _decode_image(src: str) -> bytes:
    """兼容三种来源：data URL、纯 base64、http(s) 直链。"""
    s = (src or "").strip()
    if not s:
        return b""
    if s.startswith("data:"):
        _, _, b64 = s.partition(",")
        return base64.b64decode(b64 + "=" * (-len(b64) % 4))
    if s.startswith(("http://", "https://")):
        if len(s) > 2048:
            return b""
        req = urllib.request.Request(s, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.read(IMAGE_MAX_BYTES + 1)
    return base64.b64decode(s + "=" * (-len(s) % 4))


def ocr_images(images: list) -> dict:
    """识别图片，返回 {"lines": [...], "error": str, "count": n}。"""
    engine = _get_ocr()
    if engine is None:
        return {"lines": [], "error": "ocr-unavailable", "count": 0}

    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return {"lines": [], "error": "ocr-unavailable", "count": 0}

    lines, used = [], 0
    for src in images[:IMAGE_MAX_COUNT]:
        try:
            raw = _decode_image(src)
            if not raw or len(raw) > IMAGE_MAX_BYTES:
                continue
            img = Image.open(io.BytesIO(raw))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            if max(img.size) < 24:          # 图标、分隔线之类，跳过
                continue
            # 小图先放大再识别：手机截屏缩过之后，上标、积分号、根号这类
            # 小符号最容易糊掉，放大 2 倍能明显减少丢字（实测上标仍可能错，
            # 所以数学题还要靠 math_guard 兜错答）。
            if max(img.size) < 1200:
                img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
            res, _ = engine(np.array(img))
            if not res:
                continue
            used += 1
            for item in res:
                text = (item[1] or "").strip()
                score = float(item[2] or 0)
                if text and score >= OCR_MIN_SCORE:
                    lines.append(text)
        except Exception:
            continue

    if not lines:
        return {"lines": [], "error": "ocr-empty" if used == 0 else "ocr-no-text", "count": used}
    return {"lines": lines, "error": "", "count": used}


_OPT_LINE_RE = re.compile(r"^[A-Za-z]\s*[.、)）:：]")
# 章节标题、题型标签这类行不是题干，混进去会污染检索词
_OCR_NOISE_RE = re.compile(
    r"(章节测验|单元测试|单元测验|课后练习|随堂练习|章节练习|在线作业|"
    r"^\s*[单多]选题\s*$|^\s*判断题\s*$|^\s*填空题\s*$|^\s*简答题\s*$|"
    r"^\s*第[一二三四五六七八九十\d]+[章节单元讲]\s*$|"
    r"^\s*(写笔记|笔记|收藏|纠错|举报|分享|评论)\s*$)"
)


_LETTER_ONLY_RE = re.compile(r"^[A-Ha-h]$")
_OPT_PREFIX_RE = re.compile(r"^([A-Ha-h])\s*[.、)）:：]\s*(.*)$")


def _repair_option_block(opts: list) -> list:
    """把 OCR 拆散的选项补齐字母。

    网课页面里被选中的选项是蓝底白字，OCR 常只认到 "A" 一行、内容一行，
    甚至整行字母丢失（只剩 "(1,1)."）。这里先按「字母行 + 内容行」合并，
    再按出现顺序给没有字母的补上，最后统一成 "A. xxx"。
    只有确实出现「字母独行」时才走这套，避免误伤正常的折行选项。
    """
    raw = [str(x or "").strip() for x in opts if str(x or "").strip()]
    if not any(_LETTER_ONLY_RE.match(x) for x in raw):
        return list(opts)
    items, pending = [], None
    for t in raw:
        if _LETTER_ONLY_RE.match(t):
            if pending is not None:
                items.append([pending, ""])
            pending = t.upper()
            continue
        m = _OPT_PREFIX_RE.match(t)
        if m and not t[len(m.group(1)):].lstrip().startswith("="):
            if pending is not None:
                items.append([pending, ""])
                pending = None
            items.append([m.group(1).upper(), m.group(2).strip()])
            continue
        if pending is not None:
            items.append([pending, t])
            pending = None
        else:
            items.append([None, t])
    if pending is not None:
        items.append([pending, ""])
    used = {l for l, _ in items if l}
    idx = 0
    for it in items:
        if it[0]:
            continue
        while idx < 8 and "ABCDEFGH"[idx] in used:
            idx += 1
        if idx >= 8:
            break
        it[0] = "ABCDEFGH"[idx]
        used.add(it[0])
    out = ["{}. {}".format(l, t).strip() for l, t in items if t]
    # 字母必须从 A 连续到底，否则说明选项缺行，宁可不认
    letters = [re.match(r"^([A-H])", x).group(1) for x in out]
    if letters != [chr(65 + i) for i in range(len(letters))]:
        return list(opts)
    return out


def split_ocr_lines(lines: list) -> tuple:
    """OCR 行拆成 (题干正文, 选项行)。选项行形如 A. xxx，标题行丢弃。"""
    stem, opts = [], []
    seen_opt = False
    for ln in lines:
        s = (ln or "").strip()
        if not s:
            continue
        # "A" 单独一行也要当选项标记：选中项是蓝底白字，字母常被切出来
        if _OPT_LINE_RE.match(s) or _LETTER_ONLY_RE.match(s):
            seen_opt = True
            opts.append(s)
        elif not seen_opt:
            if _OCR_NOISE_RE.search(s):
                continue
            stem.append(s)
        else:
            if _OCR_NOISE_RE.search(s):     # 「写笔记」这类按钮文字会打断选项字母连续性
                continue
            opts.append(s)      # 选项折行，归到选项区
    return "".join(stem).strip(), _repair_option_block(opts)


def merge_ocr_into_keyword(keyword: str, ocr_text: str) -> str:
    """把 OCR 文字拼进题干：有 [图片N] 占位符就替换，否则追加（已重复则不追加）。"""
    kw = (keyword or "").strip()
    text = (ocr_text or "").strip()
    if not text:
        return kw
    if not kw:
        return text
    if re.search(r"\[图片\d+\]", kw):
        return re.sub(r"\[图片\d+\]", text, kw, count=1)
    if _is_placeholder(kw):
        return text
    # 题干文字和 OCR 结果高度重合时（图片只是把同一段文字又渲染了一遍），不重复追加
    a, b = normalize(kw), normalize(text)
    if a and b and (b in a or a in b):
        return kw
    if a and b and len(bigrams(a) & bigrams(b)) / max(len(bigrams(b)), 1) >= 0.8:
        return kw
    return kw + " " + text


async def extract_images(request: Request) -> list:
    """从 POST body 取图片数组。OCS 传的是 env.images（base64 数组）。"""
    if request.method != "POST":
        return []
    try:
        raw = await request.body()
        if not raw:
            return []
        payload = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    val = payload.get("images") or payload.get("image") or payload.get("imgs")
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list):
        return []
    out = []
    for it in val:
        if isinstance(it, str) and it.strip() and not _is_placeholder(it):
            out.append(it.strip())
    return out


def _split_options(val) -> list:
    """把 options 统一成 ["A. xx","B. yy",...] 形式。

    客户端可能给数组（不带字母）、换行字符串、或 "A.xx B.yy" 这种连写。
    """
    if isinstance(val, list):
        items = [str(x).strip() for x in val if str(x).strip()]
        if items and not any(re.match(r"^[A-Za-z]\s*[.、)）:：]", it) for it in items):
            items = ["{}. {}".format("ABCDEFGH"[i], it) for i, it in enumerate(items[:8])]
        return items
    s = str(val or "").strip()
    if not s:
        return []
    parts = [p.strip() for p in re.split(r"[\n\r]+|(?<=[；;])\s*", s) if p.strip()]
    if len(parts) < 2:
        parts = [p.strip() for p in re.split(r"\s{2,}", s) if p.strip()]
    return parts


async def extract_options(request: Request) -> list:
    """从请求里取选项。OCS 的 options 走 data.env.options，纯文字题也有。"""
    if request.method == "POST":
        try:
            payload = json.loads((await request.body()).decode("utf-8", "ignore"))
            if isinstance(payload, dict):
                val = payload.get("options") or payload.get("option") or payload.get("opts")
                got = _split_options(val)
                if got:
                    return got
        except Exception:
            pass
    return _split_options(request.query_params.get("options") or "")


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
            "POST /search  {\"q\":\"关键词\",\"images\":[\"data:image/png;base64,...\"]}",
            "GET /api/daxue/meta",
            "GET /api/daxue/sources",
            "GET /api/daxue/questions?module=&q=&limit=20",
            "GET /api/daxue/random?count=5",
            "POST /api/daxue/check",
            "GET /api/daxue/export",
            "GET /subscribe",
            "POST /ocr  {\"images\":[...]}",
            "GET /math",
        ],
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "local": len(DX_QUESTIONS),
        "sources": len(SOURCES),
        "version": PROVIDER["version"],
        "ocr": ocr_available(),
        "math": math_engine_available(),
    }


@app.get("/ocr")
def ocr_help():
    return {
        "available": ocr_available(),
        "engine": "rapidocr-onnxruntime（未安装则图片题不可用）",
        "install": "pip install rapidocr-onnxruntime",
        "usage": "POST /ocr  body: {\"images\":[\"data:image/png;base64,...\"]}",
        "limits": {"max_images": IMAGE_MAX_COUNT, "max_bytes": IMAGE_MAX_BYTES, "min_score": OCR_MIN_SCORE},
        "note": "/search 收到 images 字段会自动先 OCR 再检索，通常不需要单独调这个接口。",
    }


@app.post("/ocr")
async def ocr_endpoint(request: Request):
    images = await extract_images(request)
    if not images:
        return {"code": 400, "msg": "缺少 images 字段", "data": None}
    got = ocr_images(images)
    if not got["lines"]:
        return {"code": 404, "msg": got["error"] or "未识别到文字", "data": {"count": got["count"], "lines": []}}
    stem, opts = split_ocr_lines(got["lines"])
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "count": got["count"],
            "lines": got["lines"],
            "stem": stem,
            "option_lines": opts,
            "options_parsed": [{"letter": l, "text": t} for l, t in _parse_option_lines(opts)],
            "math": is_math(stem),
        },
    }


@app.get("/math")
def math_help():
    return {
        "engine": "sympy",
        "available": math_engine_available(),
        "install": "pip install sympy",
        "kinds": [
            "极限 lim(x→a) expr",
            "定积分 ∫_a^b expr dx（区间内有瑕点时按广义积分算）",
            "极值 y=f(x) 的极小值/极大值",
            "行列式 |A|（|[1,2;3,4]| 或 |1 2; 3 4| 写法均可，二维、三维）",
            "导数 f'(x0)、切线斜率",
            "偏导数 ∂z/∂x",
            "组合概率（n 个某色球 m 个另一色，任取 k 个，至少/恰好/至多/全是）",
            "线性映射求原像 f:(x,y)→(ax+by, cx+dy)，给定像点求原像",
            "映射个数（有限集 A、B，A 到 B 的映射个数 = |B|^|A|）",
            "级数敛散（下列级数中收敛/发散的是，逐项求和判别）",
            "极值/拐点判别（y=x³ 在 x=0 处是极大、极小还是拐点）",
            "判断题命题校验（定积分等式、极限等式、某点可导/连续、具体级数敛散）",
        ],
        "policy": "数学题一律不走在线源：能解析出表达式就本地算，算出的值必须与某个选项完全一致才返回；"
                  "文字型命题先由判断题校验器验算；两者都没有就查本地高数题库；仍然没有则 404。"
                  "在线源对高数题实测会给出错答案（微分方程通解、无穷小判断都被答错过），"
                  "所以宁可留空，也不填一个错的。",
        "limits": "只覆盖可解析的表达式题与可验算的命题；几何证明、应用题建模、含参讨论不在范围内。"
                  "定理型判断（若 f'(x0)=0 则 x0 是极值点）不做逻辑推理，靠本地高数题库命中；"
                  "库里没有就返回未收录。",
        "usage": "POST /search  body: {\"q\":\"题干\",\"images\":[\"data:image/png;base64,...\"]}",
    }


# ---------------- 检索入口 ----------------
@app.api_route("/search", methods=["GET", "POST", "OPTIONS"])
@app.api_route("/search/{path_kw:path}", methods=["GET", "POST", "OPTIONS"])
async def search(request: Request, path_kw: str = None):
    if request.method == "OPTIONS":
        return JSONResponse({"ok": True})

    keyword = await extract_keyword(request, path_kw)
    images = await extract_images(request)
    req_opts = await extract_options(request)
    ocr_info = None
    ocr_opts = []
    if images:
        got = ocr_images(images)
        if got["lines"]:
            stem, opt_lines = split_ocr_lines(got["lines"])
            ocr_opts = opt_lines
            keyword = merge_ocr_into_keyword(keyword, stem or " ".join(got["lines"]))
            ocr_info = {
                "count": got["count"],
                "lines": got["lines"][:10],
                "stem": stem[:160],
                "option_lines": opt_lines[:6],
                "math": is_math(stem),
            }
        else:
            ocr_info = {"count": got["count"], "error": got["error"], "lines": []}

    if not keyword:
        if images:
            err = (ocr_info or {}).get("error")
            if err == "ocr-unavailable":
                return {
                    "code": 501,
                    "msg": "收到图片，但服务端未安装 OCR 依赖（rapidocr-onnxruntime），图片题无法识别。",
                    "data": None,
                    "ocr": ocr_info,
                }
            return {
                "code": 422,
                "msg": "收到了图片，但没能从图里识别出文字（{}），无法检索。".format(err or "ocr-empty"),
                "data": None,
                "ocr": ocr_info,
            }
        return {
            "code": 400,
            "msg": "缺少关键词。GET 用 ?q=关键词，POST 用 {\"q\":\"关键词\"}。",
            "data": None,
            "ocr": ocr_info,
        }

    # 图片只认出几个碎片字符时，拿去查在线源会命中完全无关的题并给答案，
    # 比不答更糟。实测一张整页试卷裁剪图只剩 "=Db=" 也能被匹配成某道题答 "20"。
    if images and _meaningful_len(keyword) < 6:
        return {
            "code": 422,
            "msg": "图片里只识别出 {} 个有效字符，不足以定位题目（公式、表格、拍摄模糊都会这样）。"
                   "请把这道题单独截图，题干和选项都框进去。".format(_meaningful_len(keyword)),
            "data": None,
            "ocr": ocr_info,
        }

    try:
        count = int(request.query_params.get("count", "1"))
    except ValueError:
        count = 1
    count = max(1, min(count, 10))

    opts = ocr_opts or req_opts
    src = (request.query_params.get("source") or "auto").strip()
    local_hit = None
    if src in ("all", "*"):
        order, multi = DEFAULT_ORDER, True
    elif src in SOURCE_BY_ID:
        order, multi = [src], False
    else:
        order, multi = DEFAULT_ORDER, False
        # 本地题是人工筛过的精确题：相似度 0.90 以上基本就是同一道题。
        # 先认它，别让在线源拿「措辞相似的另一道题」盖掉准确答案 ——
        # 实测「理想的追求和实现是一个____」被在线源答成「艰苦奋斗的过程」。
        # 本地没有（数学题、专业课、通识题）再按 不挂科 → 言溪 的顺序走。
        local_hit = pick_bank_candidate(bank_search(keyword, 4), opts, 0.90)
        if local_hit is None and images:
            order = ["buguake", "icodef"]

    # 数学题：能算出结果就直接用。
    # 在线源是人文社科网课题库，"差一个系数"的题会给出错答案
    # （实测 lim(x→0) sin3x/x 被匹配到 [(sin(x)-x)/x^3]，相似度 0.80，
    #  答案是 -1/6 而不是 3）。算出来的值与选项精确相等，比模糊相似度可靠。
    math_hit = None
    if opts and is_math(keyword):
        math_hit = solve_math(keyword, opts)

    if math_hit:
        payloads = [math_payload(keyword, math_hit, opts)]
        trace = [{"source": "mathsolve", "name": SOURCE_BY_ID["mathsolve"]["name"],
                  "hits": 1, "top": 1.0, "used": True,
                  "method": math_hit["method"], "matched": "math-exact"}]
        used = "mathsolve"
    elif local_hit:
        lp = local_payload(local_hit[1])
        payloads = [lp]
        used = lp["source_id"]
        trace = [{"source": used, "name": SOURCE_BY_ID[used]["name"], "hits": 1,
                  "top": round(local_hit[0], 3), "used": True, "matched": "local-exact"}]
    else:
        if is_math(keyword):
            # 数学题一律不走在线源：实测在线源会把高数题答成另一道题的答案
            # （微分方程通解 y=x²+C 被答成 y=2x+C），错误答案比留空更糟。
            payloads, trace, used = [], [], None
        else:
            # 走到这里说明不是「能算的数学题」：在线源优先（订阅本身就是靠这两个源吃饭的），
            # 本地库放在在线源之后，当作「在线没有」时的补位。
            payloads, trace, used = multi_search(
                keyword, count, [s for s in order if s not in ("local", "gaoshu")])
        if not payloads and opts:
            sol = solve_math(keyword, opts)
            if sol:
                payloads = [math_payload(keyword, sol, opts)]
                trace.append({"source": "mathsolve", "name": SOURCE_BY_ID["mathsolve"]["name"],
                              "hits": 1, "top": 1.0, "used": True, "method": sol["method"]})
                used = "mathsolve"
        if not payloads:
            local_hit = pick_bank_candidate(bank_search(keyword, 4), opts, 0.72)
            if local_hit:
                lp = local_payload(local_hit[1])
                payloads = [lp]
                trace.append({"source": lp["source_id"], "name": SOURCE_BY_ID[lp["source_id"]]["name"],
                              "hits": 1, "top": round(local_hit[0], 3), "used": True})
                used = lp["source_id"]
        if not payloads and opts and _is_judge_options(_parse_option_lines(opts)):
            rows_local = judge_bank_search(keyword, 2)
            if rows_local:
                lp = local_payload(rows_local[0][1])
                payloads = [lp]
                trace.append({"source": lp["source_id"], "name": SOURCE_BY_ID[lp["source_id"]]["name"],
                              "hits": 1, "top": round(rows_local[0][0], 3), "used": True,
                              "matched": "judge-core"})
                used = lp["source_id"]

    for p in payloads:
        fix_answer_letter(p, opts)
        remap_answer_by_option_text(p, opts, is_math(keyword))
    bad = [p for p in payloads if p.get("_inconsistent")]
    if bad:
        payloads = [p for p in payloads if not p.get("_inconsistent")]
        trace.append(
            {
                "source": used or "",
                "name": "选项校验",
                "hits": len(bad),
                "top": 0.0,
                "used": False,
                "note": "在线源答案的选项文本与本页选项对不上，已丢弃，避免填错",
            }
        )

    if not payloads:
        msg = "各题库源均未匹配到足够相似的题目"
        if is_math(keyword):
            msg += "（数学题要求数字与表达式一致，宁可判未收录，也不给可能算错的答案）"
        body = {"code": 404, "msg": msg, "keyword": keyword, "data": None, "trace": trace}
        if ocr_info:
            body["ocr"] = ocr_info
        return body

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
    if is_math(keyword):
        body["math"] = {"detected": True, "variants": math_variants(keyword)}
    if ocr_info:
        body["ocr"] = ocr_info
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
    # 两个本地库共用同一个详情接口，前端不用区分题目来自哪一份
    item = DX_BY_ID.get(question_id) or GS_BY_ID.get(question_id)
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
    item = DX_BY_ID.get(payload.id) or GS_BY_ID.get(payload.id)
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
        "count": len(DX_QUESTIONS) + len(GS_QUESTIONS),
        "breakdown": {"daxue": len(DX_QUESTIONS), "gaoshu": len(GS_QUESTIONS)},
        "license": PROVIDER["license"],
        "sources": [{"id": s["id"], "name": s["name"], "priority": s["priority"]} for s in SOURCES],
        "questions": BANK_QUESTIONS,
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
        "count": len(DX_QUESTIONS) + len(GS_QUESTIONS),
        "sources": [
            {
                "id": s["id"], "name": s["name"], "via": s["via"],
                "priority": s["priority"], "online": s["online"], "returns": s["returns"],
            }
            for s in SOURCES
        ],
        "resolution": (
            "检索顺序：数学题不走在线源——先本地符号计算（算出的值须与选项完全一致），"
            "再查本地高数题库，都没有就返回未收录；文字题按 高相似度本地题 → 不挂科（GitHub）"
            " → 言溪（GitHub）→ 本地题库 的顺序。可用 ?source=buguake|icodef|local|gaoshu|all 指定。"
        ),
        "modules": [{"id": m["id"], "name": m["name"], "count": m["count"]} for m in DX_DATA["modules"]],
    }


def _env_title() -> str:
    # 5.x 有 suggestion_title（图片已转占位符的题干）时优先用它，老版本回落到 title
    return "return (env) => (env.suggestion_title || env.title || '')"


def _env_options() -> str:
    return "return (env) => (env.suggestion_options || env.options || '')"


def _env_images() -> str:
    # 第一优先：OCS 5.x 已把题干图片转成 base64 塞进 env.images（不受防盗链/跨域影响）。
    # 回落：从 DOM 里抓题干区域的 img，交给服务端下载。
    # 注意 OCS 靠字符串里是否出现 env.images 来判断「这题库支持图片题」，
    # 所以这一行必须保留，否则面板会提示「暂不支持图片、公式等题目处理」。
    return (
        "return (env) => {"
        " if (env.images && env.images.length) return env.images;"
        " const sel = '.questionLi img, .TiMu img, .mark_item img, .stem_answer img, .subject_describe img';"
        " const imgs = [...document.querySelectorAll(sel)];"
        " return imgs.slice(0, 3).map(i => i.src).filter(Boolean); }"
    )


def build_subscription(base: str, sharecode: str) -> list:
    """OCS 网课助手专用格式，支持图片题。

    要点（来自 OCS 5.x 实际协议）：
      - 请求体字段是 data，不是 body；
      - data 的值可以是 {handler} 对象，OCS 会传 env 进去求值；
      - 必须 POST，且 data 里要出现 env.images 引用，否则 OCS 判定不支持图片题；
      - 答案多选用 # 分隔，未命中返回 undefined。
    """
    auth = "sharecode={}".format(sharecode) if sharecode else ""
    suffix = ("?" + auth) if auth else ""
    common = _common_meta(base, auth)
    handler = _ocs_handler()
    data_single = {
        "title": {"handler": _env_title()},
        "options": {"handler": _env_options()},
        "type": "${type}",
        "images": {"handler": _env_images()},
    }
    return [
        {
            "name": "大学知识题库（OCS · 单结果 · 支持图片题）",
            "url": "{}/search{}".format(base, suffix),
            "method": "post",
            "contentType": "json",
            "type": "GM_xmlhttpRequest",
            "headers": {"Accept": "application/json", "Content-Type": "application/json"},
            "data": data_single,
            "handler": handler,
            **common,
        },
        {
            "name": "大学知识题库（OCS · 多结果 · 支持图片题）",
            "url": "{}/search?count=3{}".format(base, ("&" + auth) if auth else ""),
            "method": "post",
            "contentType": "json",
            "type": "GM_xmlhttpRequest",
            "headers": {"Accept": "application/json", "Content-Type": "application/json"},
            "data": data_single,
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
