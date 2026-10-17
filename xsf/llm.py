"""xsf.llm — 纯文本 LLM provider (P2c 翻译模型接入, 2026-10-17).

多行制转录 (1/3/4 行) 的 translit/gloss/trans 派生层由翻译大模型生成草稿,
人工修订后经 edit_page 正常保存 (修订史分层记 + AI 披露入 basis).

- chat(): OpenAI 兼容 chat/completions 薄封装 (provider 来自 config v3
  type='llm_chat', default 走 cfg['default_llm'] 独立解析链)
- autofill_rows(): 页级批量生成 — LGR word 级词对照/流畅译文/拉丁转写,
  整页一次请求, JSON 返回, 数量对齐校验

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
不要任何解释文字。未被要求的字段直接省略。"""


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
    resp.raise_for_status()
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


def autofill_rows(blocks: list[dict], src_lang: str,
                  targets: list[str], provider_id: str = None,
                  ) -> tuple[list[dict], str]:
    """页级批量生成 draft 层。

    blocks: [{text: 原文(原语或拉丁化), gloss?, trans?, translit?}, ...]
    targets: ['translit', 'gloss', 'trans'] 子集
    返回 (results, model): results 与 blocks 等长, 每项只含 targets 中字段。
    """
    if not blocks:
        return [], ''
    targets = [t for t in targets if t in ('translit', 'gloss', 'trans')]
    if not targets:
        raise ValueError('targets 至少含 translit/gloss/trans 之一')
    user_prompt = (
        f"源语种码: {src_lang}\n"
        f"需要生成的字段: {', '.join(targets)}\n"
        f"文本块数组:\n{json.dumps(
            [{'text': b.get('text', '')} for b in blocks],
            ensure_ascii=False)}\n"
    )
    content, model = chat(
        [{'role': 'system', 'content': AUTOFILL_SYSTEM_PROMPT},
         {'role': 'user', 'content': user_prompt}],
        provider_id=provider_id,
    )
    data = _extract_json_array(content)
    if not isinstance(data, list) or len(data) != len(blocks):
        raise ValueError(
            f'LLM 返回数量不对齐: 期望 {len(blocks)} 块, 得到 '
            f'{len(data) if isinstance(data, list) else type(data).__name__}'
        )
    results = []
    for item in data:
        row = {}
        for t in targets:
            v = str(item.get(t, '') or '').strip()
            if v:
                row[t] = v
        results.append(row)
    return results, model
