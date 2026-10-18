"""xsf.llm — 纯文本 LLM provider (P2c 翻译模型接入, 2026-10-17).

多行制转录 (1/3/4 行) 的 translit/gloss/trans 派生层由翻译大模型生成草稿,
人工修订后经 edit_page 正常保存 (修订史分层记 + AI 披露入 basis).

- chat(): OpenAI 兼容 chat/completions 薄封装 (provider 来自 config v3
  type='llm_chat', default 走 cfg['default_llm'] 独立解析链)
- autofill_rows(): 页级批量生成 — LGR word 级词对照/流畅译文/拉丁转写,
  分批请求 (每批 ≤6 块且 ≤1200 字符, 批内数量对齐校验 + 失败重试 1 次,
  再失败该批空行占位), 2026-10-18 改 (整页一次请求长文本丢项不稳)

设计契约: mem 小书房/design/完善路线图.md P2c (用户裁定 2026-10-17:
全实现/页级/落库 draft 态)。
"""
import json
import re

import requests

from .config import get_llm_provider_cfg


class LLMNotConfigured(RuntimeError):
    """未配置 llm_chat provider。"""


AUTOFILL_SYSTEM_PROMPT = """\
你是文献翻译引擎。输入是文献文本块数组 (JSON)。对每个文本块, 按要求生成目标\
字段, 以 JSON 数组返回 — 数量、顺序必须与输入完全一致。

字段要求:
- translit (拉丁转写): 将原语文本转写为拉丁字母。保留源语语序, 词间空格分隔; \
已有拉丁字符的部分原样保留。仅当原语非拉丁字母文字系统时提供。
- gloss (词语对照): 逐词对照翻译 — 保持源语语序 (错位排列是特性), 每个源词对\
应一个目标词 (中文), 词间空格分隔。参照莱比锡标注惯例 word 级。
- trans (流畅译文): 通顺流畅的中文翻译, 可按中文语序重组。

只输出 JSON 数组 (如 [{"translit":"...","gloss":"...","trans":"..."}]), \
不要任何解释文字。未被要求的字段直接省略。

硬性要求: 数组中每个对象对应输入的一个文本块, 顺序一致、一个不漏; \
被要求的字段每块都必须给出 (实在无法确定也给出最佳猜测), 不得留空、\
不得省略对象、不得合并相邻块。"""


def chat(messages: list[dict], provider_id: str = None,
         max_tokens: int = None, timeout: float = 120.0) -> tuple[str, str]:
    """调用 LLM (OpenAI 兼容)。返回 (content, model_name)。"""
    try:
        cfg = get_llm_provider_cfg(provider_id)
    except RuntimeError as e:
        raise LLMNotConfigured(str(e))
    url = cfg.get('base_url', '')
    if not url.endswith('/chat/completions'):
        url += '/chat/completions'
    headers = {'Content-Type': 'application/json'}
    if cfg.get('api_key'):
        headers['Authorization'] = f"Bearer {cfg['api_key']}"
    body = {
        'model': cfg.get('model') or 'glm-4-flash',
        'messages': messages,
        'stream': False,
    }
    if max_tokens:
        body['max_tokens'] = max_tokens
    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        # 透传服务端错误详情 (模型格式限制/鉴权/参数等诊断关键)
        try:
            err = resp.json()
            detail = (err.get('error') or {}).get('message') or resp.text
        except (ValueError, AttributeError):
            detail = resp.text
        raise RuntimeError(
            f'LLM 请求失败 HTTP {resp.status_code}: {str(detail)[:400]} '
            f'(model={body["model"]}; 专用翻译模型如 qwen-mt 仅支持固定 '
            f'Translate 指令模式, 请改用通用对话模型)')
    data = resp.json()
    content = data['choices'][0]['message']['content']
    return content, data.get('model', body['model'])


def _extract_json_array(text: str) -> list:
    """容错提取 JSON 数组: 裸数组 / ```json 围栏 / 前后杂文。"""
    text = text.strip()
    m = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.S)
    if m:
        text = m.group(1)
    else:
        m = re.search(r'\[.*\]', text, re.S)
        if m:
            text = m.group(0)
    return json.loads(text)


def _autofill_batch(batch: list[dict], src_lang: str, targets: list[str],
                    provider_id: str = None):
    """单批生成: 返回 (rows, model), 数量不对齐/解析失败返回 None。"""
    user_prompt = (
        f"源语种码: {src_lang}\n"
        f"需要生成的字段: {', '.join(targets)}\n"
        f"文本块数组:\n{json.dumps(
            [{'text': b.get('text', '')} for b in batch],
            ensure_ascii=False)}\n"
    )
    content, model = chat(
        [{'role': 'system', 'content': AUTOFILL_SYSTEM_PROMPT},
         {'role': 'user', 'content': user_prompt}],
        provider_id=provider_id,
    )
    data = _extract_json_array(content)
    if not isinstance(data, list) or len(data) != len(batch):
        return None
    rows = []
    for item in data:
        row = {}
        for t in targets:
            v = str(item.get(t, '') or '').strip()
            if v:
                row[t] = v
        rows.append(row)
    return rows, model


def autofill_rows(blocks: list[dict], src_lang: str,
                  targets: list[str], provider_id: str = None,
                  batch_blocks: int = 6, batch_chars: int = 1200,
                  ) -> tuple[list[dict], str]:
    """页级批量生成 draft 层 (分批: 稳定性优先).

    blocks: [{text: 原文(原语或拉丁化), gloss?, trans?, translit?}, ...]
    targets: ['translit', 'gloss', 'trans'] 子集
    返回 (results, model): results 与 blocks 等长, 每项只含 targets 中字段。

    长文本整页一次请求易丢项 (gloss 缺失/数量不齐), 故按批生成:
    每批 ≤ batch_blocks 块且累计原文 ≤ batch_chars 字符 (先到为准);
    单批失败自动重试 1 次, 再失败该批置空行 (不炸整页, 可重按补缺)。
    """
    if not blocks:
        return [], ''
    targets = [t for t in targets if t in ('translit', 'gloss', 'trans')]
    if not targets:
        raise ValueError('targets 至少含 translit/gloss/trans 之一')

    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for b in blocks:
        t_len = len(b.get('text', '') or '')
        if cur and (len(cur) >= batch_blocks or cur_chars + t_len > batch_chars):
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(b)
        cur_chars += t_len
    if cur:
        batches.append(cur)

    results: list[dict] = []
    model = ''
    failed_batches = 0
    for batch in batches:
        got = None
        for _attempt in range(2):  # 失败重试 1 次
            try:
                got = _autofill_batch(batch, src_lang, targets, provider_id)
            except Exception:
                got = None
            if got:
                break
        if got:
            results.extend(got[0])
            model = got[1]
        else:
            results.extend([{} for _ in batch])
            failed_batches += 1
    if failed_batches:
        # 部分批次失败: 空行占位, 前端再按一次只补缺 (值不变跳过)
        pass
    return results, model
