"""文件夹投放自动入库 + PDF 自动 OCR.

后台线程轮询各书架 uploads/ 目录 (NFS 挂载无 inotify, 只能轮询 diff):
  - 新 PDF/MD 自动入库 (秒级解析, 文献列表立即可见)
  - PDF 不区分 born-digital 一律入自动 OCR 队列 (2026-09-04 决策),
    OCR worker 单线程串行回填 (与手动 reocr 走同一 job store / 执行器);
    幂等闸门: 已有任一行文本即出队 (doc_type 不作凭证, 2026-11-20 修)
  - 投放重复 (filename 已在 DB) 跳过并记录到状态角标; 新入库文件若与
    其它书架同名, 记录跨书架警告 (仅提醒不阻断, 跨书架共存是设计行为)

环境变量:
  XSF_SCAN_INTERVAL    轮询间隔秒 (默认 120, 0=关闭整个扫描)
  XSF_SCAN_STABLE_SEC  文件稳定阈值秒 (默认 60, mtime 距今小于此值视为仍在写入)
  XSF_SCAN_OCR         自动 OCR (默认 1, 0=仅标记待OCR 不自动跑)

投放规则: 文件放 {书架}/uploads/ 才会被认领; collections 根目录与 _orphan/ 不扫.
"""

import logging
import os
import threading
import time
from pathlib import Path

import pymupdf

from .config import (
    get_collections_dir, list_collections,
    find_orphan_collections,
)
from .db import get_conn
from .ingest import (
    ingest_native_pdf, ingest_markdown, get_original_file,
)
from .reocr import (
    _reocr_get, _reocr_jobs, _reocr_lock, _run_reocr_doc, pop_job,
    register_job,
)

logger = logging.getLogger("xsf.scan")

# 2026-10-31 格式收敛: 投放只认 pdf/md (epub/Word 通道退役, 外部转 PDF)
_SCAN_EXTS = {'.pdf', '.md', '.markdown'}
_IGNORE_SUFFIXES = ('.tmp', '.part', '.partial', '.crdownload', '.swp')
MAX_OCR_ATTEMPTS = 3
_DUP_REPORT_WINDOW = 3600   # mtime 距今小于此值的重复文件才报「跳过重复」(秒)

_stop = threading.Event()
_queue_cond = threading.Condition()
_ocr_queue: list = []                     # [(collection, doc_id, kind)] kind: native|ocr
_ocr_attempts: dict = {}                  # (collection, doc_id) -> 失败次数
_status_lock = threading.Lock()
_status: dict = {}                        # collection -> 状态快照
_failed: dict = {}                        # (collection, filename) -> (mtime, error) 坏文件防重试噪音


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def interval() -> int:
    return _env_int('XSF_SCAN_INTERVAL', 120)


def _is_ignored(fn: str) -> bool:
    return (fn.startswith('.') or fn.startswith('~$')
            or fn.lower().endswith(_IGNORE_SUFFIXES))


def _upload_dir(collection: str) -> Path:
    """被动取书架 uploads 目录 (不 mkdir, 目录不存在=没有可扫文件)."""
    return get_collections_dir() / collection / 'uploads'


def _cross_collections(filename: str, current: str) -> list:
    """查同名文件存在于哪些其它书架 (仅提醒)."""
    hits = []
    for c in list_collections():
        if c == current:
            continue
        try:
            rows = _query(
                c, 'SELECT id FROM documents WHERE filename = ?', (filename,))
        except Exception:
            continue
        if rows:
            hits.append(c)
    return hits


def _ingest_new(collection: str) -> tuple[list, list, list]:
    """diff 目录 vs DB, 新文件入库. 返回 (new_records, errors, dups)."""
    updir = _upload_dir(collection)
    if not updir.is_dir():
        return [], [], []
    try:
        db_names = {
            r['filename'] for r in _query(
                collection, 'SELECT filename FROM documents')}
    except Exception:
        return [], [], []

    stable_sec = _env_int('XSF_SCAN_STABLE_SEC', 60)
    now = time.time()
    new_records, errors, dups = [], [], []

    for f in sorted(updir.iterdir()):
        fn = f.name
        if not f.is_file() or _is_ignored(fn):
            continue
        if f.suffix.lower() not in _SCAN_EXTS:
            continue
        if fn in db_names:
            # 新投放的重复 (mtime 在窗口内) 报告一次; 陈年文件不刷屏
            if now - f.stat().st_mtime < _DUP_REPORT_WINDOW:
                dups.append(fn)
                logger.info("跳过重复 coll=%s %s (已在库)", collection, fn)
            _failed.pop((collection, fn), None)
            continue
        if now - f.stat().st_mtime < stable_sec:
            continue  # 仍在写入, 下轮再收
        prev = _failed.get((collection, fn))
        if prev and prev[0] == f.stat().st_mtime:
            continue  # 坏文件未变化, 不重试 (防每轮刷错误)

        try:
            if f.suffix.lower() == '.pdf':
                # 2026-10-31 管线收敛: 强制 anydoc; 整篇不可转 → 建空文档
                # (无行) 入自动 OCR 队列 (_pending_ocr 兜底)
                try:
                    r = ingest_native_pdf(f, collection=collection)
                except Exception as e:
                    logger.warning(
                        'anydoc 不可转 %s: %s — 建空文档待自动 OCR 队列',
                        f.name, e)
                    doc = pymupdf.open(f)
                    pages = len(doc)
                    doc.close()
                    conn = get_conn(collection)
                    cur = conn.execute(
                        # doc_type='born-digital' + 0 行 正是 _pending_ocr
                        # 的入队条件 (doc_type NULL 会被 WHERE 判掉)
                        "INSERT INTO documents (filename, page_count, doc_type) "
                        "VALUES (?, ?, 'born-digital')",
                        (f.name, pages))
                    conn.commit()
                    conn.close()
                    r = {'doc_id': cur.lastrowid, 'pages': pages,
                        'note': f'anydoc 不可转 ({e}), 待自动 OCR 队列'}
                r['ext'] = 'pdf'
            else:
                r = ingest_markdown(f, collection=collection)
                r['ext'] = 'md'
            # 原件统一模型 (2026-10-28): 投放入库即把原件移入
            # files/<id>/original.<ext> (移入式, 不留双份)
            from .ingest import attach_original
            attach_original(collection, r['doc_id'], f, move=True)
            r['filename'] = fn
            # PDF 提取零行才入 OCR 队列 (2026-10-21 决策;
            # 有文本层的不重跑, 防覆盖优质原生提取)
            r['needs_ocr'] = r['ext'] == 'pdf' and r.get('lines', 0) == 0
            r['cross'] = _cross_collections(fn, collection)
            if r['cross']:
                logger.warning("跨书架同名 coll=%s %s 也存在于: %s",
                               collection, fn, ', '.join(r['cross']))
            new_records.append(r)
            _failed.pop((collection, fn), None)
            logger.info("扫描入库 coll=%s %s: %s 页 %s 行%s",
                        collection, fn, r['pages'], r['lines'],
                        ' (待OCR)' if r['needs_ocr'] else '')
        except Exception as e:
            _failed[(collection, fn)] = (f.stat().st_mtime, str(e))
            errors.append({'filename': fn, 'error': str(e)})
            logger.warning("扫描入库失败 coll=%s %s: %s", collection, fn, e)
    return new_records, errors, dups


def _missing(collection: str) -> list:
    """反向 diff: DB 有记录但原件已消失. 只报告不删除 (2026-09-19 决策,
    库是事实源, 删原件≠删记录; 清理走 purge-missing 接口人工确认).

    2026-10-28 原件统一模型: 原件在 files/<id>/ 的文档视为在场;
    无原件文档 (original_virtual=1) 不报。
    """
    updir = _upload_dir(collection)
    try:
        rows = _query(
            collection,
            "SELECT id, filename, linked_pdf, original_virtual "
            "FROM documents WHERE filename != ''")
    except Exception:
        return []
    present: set = set()
    if updir.is_dir():
        present = {f.name for f in updir.iterdir() if f.is_file()}
    cdir = get_collections_dir() / collection
    out = []
    for r in rows:
        if r['original_virtual']:
            continue
        lp = r['linked_pdf'] or ''
        if lp.startswith('files/'):
            if (cdir / lp).is_file():
                continue
        elif lp and (updir / lp).is_file():
            continue
        if r['filename'] in present:
            continue
        out.append({'id': r['id'], 'filename': r['filename']})
    return out


def _query(collection: str, sql: str, params=()):
    conn = get_conn(collection)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _pending_ocr(collection: str) -> list:
    """尚无可用文本的 PDF, 供入队.

    判定 = 只要一行文本都没有就待 OCR, 不看 doc_type (2026-11-20 修):
    旧闸门 `doc_type != 'ocr'` 把「标了 ocr 却零行」的文献永久挡在队列外
    —— OCR 调用返回空块 / 中途失败仍在收尾时写 doc_type='ocr' 都会留下
    这种文档 (生产实测 13 本, 无一能再入队). doc_type 是管线标签不是成功
    凭证, 唯一可信的成功信号是有行.
    已有文本的一律不入队 (2026-10-21 决策, 修正 2026-09-04「PDF 一律
    入队」— 实测自动 OCR 反而覆盖更优的原生文本层).
    幂等: OCR 出文本后不再返回; 反复零结果的由 MAX_OCR_ATTEMPTS 兜底.
    页级失败 (ocr_page_state.status='error') 也重新排队: 断点续跑只补
    错误页, 已有文本页跳过 (2026-11-20).
    """
    rows = _query(collection, """
        SELECT d.id, d.filename, d.page_count
        FROM documents d
        WHERE d.filename LIKE '%.pdf'
          AND d.doc_type != 'pending'
          AND (NOT EXISTS (SELECT 1 FROM lines l WHERE l.doc_id = d.id)
               OR EXISTS (SELECT 1 FROM ocr_page_state s
                          WHERE s.doc_id = d.id AND s.status = 'error'))
    """)
    return [{'id': r['id'], 'filename': r['filename'],
             'pages': r['page_count']} for r in rows]


def _enqueue_extract(collection: str, doc_id: int, kind: str) -> bool:
    """提取任务入队 (2026-11-26 先入库后提取)。kind: native | ocr。

    幂等: 已在队 / 正在跑 / 超尝试上限 → 不入队。返回是否入队。
    """
    key = (collection, doc_id, kind)
    with _queue_cond:
        if key in set(_ocr_queue):
            return False
        if _ocr_attempts.get(key, 0) >= MAX_OCR_ATTEMPTS:
            return False
        job = _reocr_get(collection, doc_id)
        if job and job.get("status") == "running":
            return False  # 手动/自动正在跑
        _ocr_queue.append(key)
        _queue_cond.notify_all()
    return True


def _enqueue_ocr(collection: str, pending: list) -> None:
    for d in pending:
        _enqueue_extract(collection, d["id"], "ocr")


_orphan_warned: set = set()            # 已警告过的投放孤儿 (集合变化时才再报)


def _warn_orphan_dirs() -> None:
    """collections/ 下有目录但无 DB → 投放永不入库且原本无任何提示.

    每扫描周期探测, 集合变化时 warning (首次出现/全部消失), 避免刷屏.
    """
    try:
        _, dir_orphans = find_orphan_collections()
    except Exception as e:
        logger.debug("孤儿目录探测失败: %s", e)
        return
    current = set(dir_orphans)
    if current == _orphan_warned:
        return
    if current:
        logger.warning(
            "发现投放孤儿目录 (无对应书架 DB, uploads/ 文件不会被入库): %s; "
            "请在 Web 书架页用同名新建书架修复", ', '.join(sorted(current)))
    elif _orphan_warned:
        logger.info("投放孤儿目录已全部修复: %s",
                    ', '.join(sorted(_orphan_warned)))
    _orphan_warned.clear()
    _orphan_warned.update(current)


def _scan_cycle() -> None:
    ocr_enabled = _env_int('XSF_SCAN_OCR', 1) != 0
    _warn_orphan_dirs()
    for coll in list_collections():
        # 单书架失败不断整轮 (2026-11-20: 一次 database is locked 会让所有
        # 书架当轮都不入队, 表现就是「扫描了但没排 OCR」)
        try:
            _scan_collection(coll, ocr_enabled)
        except Exception as e:
            logger.warning("扫描周期异常 coll=%s: %s", coll, e)
            with _status_lock:
                st = _status.setdefault(coll, {})
                st['errors'] = (st.get('errors') or [])[-2:] + [
                    {'filename': '(扫描周期)', 'error': str(e)}]


def _scan_collection(coll: str, ocr_enabled: bool) -> None:
    new_records, errors, dups = _ingest_new(coll)
    # pending 滞留兜底 (2026-11-26: add 先入库后提取, worker 崩溃时
    # 扫描周期负责重新入队; _enqueue_extract 幂等去重)
    for r in _query(coll, "SELECT id FROM documents "
                          "WHERE doc_type = 'pending'"):
        _enqueue_extract(coll, r['id'], 'native')
    pending = _pending_ocr(coll)
    if ocr_enabled:
        _enqueue_ocr(coll, pending)
    cross_warn = [
        {'filename': r['filename'], 'also_in': r['cross']}
        for r in new_records if r.get('cross')]
    missing = _missing(coll)
    with _status_lock:
        _status[coll] = {
            'last_run': time.strftime('%H:%M:%S'),
            'added': len(new_records),
            'new': [r['filename'] for r in new_records[:5]],
            'dup_total': len(dups),
            'dups': dups[:5],
            'cross_warn': cross_warn[:3],
            'pending_ocr': pending,
            'missing': [m['filename'] for m in missing[:10]],
            'missing_total': len(missing),
            'errors': [
                {'filename': e['filename'], 'error': e['error']}
                for e in errors[:3]],
        }


def _scan_loop(interval: int) -> None:
    logger.info("文件夹扫描线程启动, 间隔 %ss", interval)
    while not _stop.is_set():
        try:
            _scan_cycle()
        except Exception as e:
            logger.warning("扫描周期异常: %s", e)
        _stop.wait(interval)
    logger.info("文件夹扫描线程退出")


def _run_native_extract(collection: str, doc_id: int, pdf_path,
                        row) -> None:
    """worker: 对 pending 文档试跑 native anydoc 提取 (2026-11-26)。

    成功 → doc_type='pdf_native' (extract_native_doc 内写);
    零行/异常 → doc_type 已置 NULL, 直接入 OCR 队列接力。
    """
    from .ingest import extract_native_doc
    if not register_job(collection, doc_id,
                        _pdf_pages(pdf_path), mode='native',
                        source='auto'):
        return  # 已有任务在跑
    to_ocr = False
    try:
        logger.info("提取开始 coll=%s doc=%s: native (anydoc)",
                    collection, doc_id)
        r = extract_native_doc(collection, doc_id, pdf_path)
        logger.info("提取完成 coll=%s doc=%s: native %s 行 %s 块 "
                    "(空页 %s, 直提兜底 %s)",
                    collection, doc_id, r['lines'], r['blocks'],
                    len(r['empty_pages']), r['needs_ocr_fallback'])
        _ocr_attempts.pop((collection, doc_id, 'native'), None)
    except Exception as e:
        logger.info("native 提取不可用 coll=%s doc=%s: %s → 转 OCR 队列",
                    collection, doc_id, e)
        to_ocr = True  # extract_native_doc 已把 doc_type 置 NULL
    finally:
        pop_job(collection, doc_id)
    # 转 OCR 必须晚于 pop_job: 否则 _enqueue_extract 见本 job 仍 running
    # 拒收 (2026-11-26 首测踩到, 文档滞留 NULL 只能等扫描周期兜底)
    if to_ocr:
        _enqueue_extract(collection, doc_id, 'ocr')


def _pdf_pages(pdf_path) -> int:
    try:
        src = pymupdf.open(str(pdf_path))
        try:
            return len(src)
        finally:
            src.close()
    except Exception:
        return 1


def _ocr_worker() -> None:
    logger.info("自动 OCR worker 启动")
    while not _stop.is_set():
        with _queue_cond:
            while not _ocr_queue and not _stop.is_set():
                _queue_cond.wait(timeout=5)
            if _stop.is_set():
                return
            collection, doc_id, kind = _ocr_queue.pop(0)

        try:
            row = _query(
                collection,
                'SELECT id, filename FROM documents WHERE id = ?', (doc_id,))
            if not row:
                _ocr_attempts.pop((collection, doc_id, kind), None)
                continue
            pdf_path = get_original_file(collection, doc_id)
            if not pdf_path:
                # 计次: 否则每轮扫描都会重新入队同一个缺原件文档, 刷日志
                key = (collection, doc_id, kind)
                n = _ocr_attempts.get(key, 0) + 1
                _ocr_attempts[key] = n
                logger.warning("自动 OCR 跳过 coll=%s doc=%s: 无原件 "
                               "(单布局 files/<id>/, 2026-10-31) (%s/%s)",
                               collection, doc_id, n, MAX_OCR_ATTEMPTS)
                continue
            src = pymupdf.open(pdf_path)
            total_pages = len(src)
            src.close()

            if kind == 'native':
                _run_native_extract(collection, doc_id, pdf_path, row)
                continue

            from .ocr import get_provider
            provider = get_provider()

            if not register_job(collection, doc_id, total_pages,
                                mode='full', source='auto'):
                continue  # 手动 reocr 正在跑
            logger.info("自动 OCR 开始 coll=%s doc=%s (%s页): %s",
                        collection, doc_id, total_pages, row[0]['filename'])
            _run_reocr_doc(collection, doc_id, provider, pdf_path, total_pages)

            job = _reocr_get(collection, doc_id)
            if job and job.get('status') == 'done_with_errors':
                # 页级断点: 大部分页已入库 (doc_type='ocr'), 失败页明细在
                # ocr_page_state, 重跑会自动跳过 done 页只补失败页.
                # 也计次: 失败页会让 _pending_ocr 每轮重新入队, 不计次就
                # 每 2 分钟重跑一次 (2026-11-20).
                key = (collection, doc_id, kind)
                n = _ocr_attempts.get(key, 0) + 1
                _ocr_attempts[key] = n
                logger.warning(
                    "自动 OCR 部分完成 coll=%s doc=%s 失败页=%s (明细见 "
                    "ocr_page_state, 第 %s/%s 次)",
                    collection, doc_id, job.get('failed_pages'),
                    n, MAX_OCR_ATTEMPTS)
            elif job and job.get('status') == 'error':
                key = (collection, doc_id, kind)
                n = _ocr_attempts.get(key, 0) + 1
                _ocr_attempts[key] = n
                logger.warning("自动 OCR 失败 coll=%s doc=%s (第 %s/%s 次): %s",
                               collection, doc_id, n, MAX_OCR_ATTEMPTS,
                               job.get('error'))
                with _status_lock:
                    st = _status.setdefault(collection, {})
                    st.setdefault('errors', []).append({
                        'filename': row[0]['filename'],
                        'error': f"自动OCR失败: {job.get('error')}"})
                    st['errors'] = st['errors'][-3:]
            else:
                _ocr_attempts.pop((collection, doc_id, kind), None)
        except Exception as e:
            pop_job(collection, doc_id)
            key = (collection, doc_id, kind)
            _ocr_attempts[key] = _ocr_attempts.get(key, 0) + 1
            logger.warning("自动 OCR worker 异常 coll=%s doc=%s: %s",
                           collection, doc_id, e)
    logger.info("自动 OCR worker 退出")


def get_scan_status(collection: str) -> dict:
    """供 docs_list 页渲染角标: 扫描时间/新增/OCR 队列/待OCR/错误."""
    with _status_lock:
        st = dict(_status.get(collection) or {})

    running = 0
    queued = 0
    with _queue_cond:
        queued = sum(1 for item in _ocr_queue if item[0] == collection)
    job = _reocr_get_all_for(collection)
    running = sum(1 for j in job.values()
                  if j.get('status') == 'running' and j.get('source') == 'auto')

    st.update({
        'enabled': _env_int('XSF_SCAN_INTERVAL', 120) > 0,
        'interval': interval(),
        'ocr_enabled': _env_int('XSF_SCAN_OCR', 1) != 0,
        'ocr_queued': queued,
        'ocr_running': running,
    })
    return st


def _reocr_get_all_for(collection: str) -> dict:
    with _reocr_lock:
        return {k: dict(v) for k, v in _reocr_jobs.items() if k[0] == collection}


_threads: list = []


def start_scan_threads() -> None:
    """lifespan 启动入口. XSF_SCAN_INTERVAL=0 时不启任何线程."""
    interval = _env_int('XSF_SCAN_INTERVAL', 120)
    if interval <= 0:
        logger.info("文件夹扫描关闭 (XSF_SCAN_INTERVAL=%s)", interval)
        return
    t1 = threading.Thread(target=_scan_loop, args=(interval,),
                          daemon=True, name='xsf-scan')
    t1.start()
    _threads.append(t1)
    if _env_int('XSF_SCAN_OCR', 1) != 0:
        t2 = threading.Thread(target=_ocr_worker,
                              daemon=True, name='xsf-scan-ocr')
        t2.start()
        _threads.append(t2)


def shutdown_scan() -> None:
    _stop.set()
    with _queue_cond:
        _queue_cond.notify_all()
