"""文件夹投放自动入库 + PDF 自动 OCR.

后台线程轮询各书架 uploads/ 目录 (NFS 挂载无 inotify, 只能轮询 diff):
  - 新 PDF/MD 自动入库 (秒级解析, 文献列表立即可见)
  - PDF 不区分 born-digital 一律入自动 OCR 队列 (2026-09-04 决策),
    OCR worker 单线程串行回填 (与手动 reocr 走同一 job store / 执行器);
    幂等闸门: doc_type='ocr' 即出队
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

from .config import get_collections_dir, get_upload_path, list_collections
from .db import get_conn
from .ingest import ingest_pdf, ingest_markdown
from .reocr import (
    _reocr_get, _reocr_jobs, _reocr_lock, _run_reocr_doc, pop_job,
    register_job,
)

logger = logging.getLogger("xsf.scan")

_SCAN_EXTS = {'.pdf', '.md', '.markdown'}
_IGNORE_SUFFIXES = ('.tmp', '.part', '.partial', '.crdownload', '.swp')
MAX_OCR_ATTEMPTS = 3
_DUP_REPORT_WINDOW = 3600   # mtime 距今小于此值的重复文件才报「跳过重复」(秒)

_stop = threading.Event()
_queue_cond = threading.Condition()
_ocr_queue: list = []                     # [(collection, doc_id)]
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
                r = ingest_pdf(f, collection=collection)
                r['ext'] = 'pdf'
            else:
                r = ingest_markdown(f, collection=collection)
                r['ext'] = 'md'
            r['filename'] = fn
            # PDF 一律入 OCR 队列 (2026-09-04 决策, 不区分 born-digital)
            r['needs_ocr'] = r['ext'] == 'pdf'
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


def _query(collection: str, sql: str, params=()):
    conn = get_conn(collection)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _pending_ocr(collection: str) -> list:
    """全库所有未 OCR 的 PDF (doc_type != 'ocr'), 供入队.

    天然幂等: OCR 完成后 doc_type→'ocr' 不再返回;
    OCR 后仍空文本的也不会死循环重试 (同样被 doc_type 闸门挡住).
    """
    rows = _query(collection, """
        SELECT d.id, d.filename, d.page_count
        FROM documents d
        WHERE d.doc_type != 'ocr' AND d.filename LIKE '%.pdf'
    """)
    return [{'id': r['id'], 'filename': r['filename'],
             'pages': r['page_count']} for r in rows]


def _enqueue_ocr(collection: str, pending: list) -> None:
    with _queue_cond:
        queued = {item for item in _ocr_queue}
        for d in pending:
            key = (collection, d['id'])
            if key in queued:
                continue
            if _ocr_attempts.get(key, 0) >= MAX_OCR_ATTEMPTS:
                continue  # 放弃, 留在状态 errors 里人工处理
            job = _reocr_get(collection, d['id'])
            if job and job.get('status') == 'running':
                continue  # 手动/自动正在跑
            _ocr_queue.append(key)
        _queue_cond.notify_all()


def _scan_cycle() -> None:
    ocr_enabled = _env_int('XSF_SCAN_OCR', 1) != 0
    for coll in list_collections():
        new_records, errors, dups = _ingest_new(coll)
        pending = _pending_ocr(coll)
        if ocr_enabled:
            _enqueue_ocr(coll, pending)
        cross_warn = [
            {'filename': r['filename'], 'also_in': r['cross']}
            for r in new_records if r.get('cross')]
        with _status_lock:
            _status[coll] = {
                'last_run': time.strftime('%H:%M:%S'),
                'added': len(new_records),
                'new': [r['filename'] for r in new_records[:5]],
                'dup_total': len(dups),
                'dups': dups[:5],
                'cross_warn': cross_warn[:3],
                'pending_ocr': pending,
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


def _ocr_worker() -> None:
    logger.info("自动 OCR worker 启动")
    while not _stop.is_set():
        with _queue_cond:
            while not _ocr_queue and not _stop.is_set():
                _queue_cond.wait(timeout=5)
            if _stop.is_set():
                return
            collection, doc_id = _ocr_queue.pop(0)

        try:
            row = _query(
                collection,
                'SELECT filename FROM documents WHERE id = ?', (doc_id,))
            if not row:
                _ocr_attempts.pop((collection, doc_id), None)
                continue
            pdf_path = get_upload_path(collection, row[0]['filename'])
            if not pdf_path.exists():
                logger.warning("自动 OCR 跳过 coll=%s doc=%s: 源文件缺失 %s",
                               collection, doc_id, row[0]['filename'])
                continue
            src = pymupdf.open(pdf_path)
            total_pages = len(src)
            src.close()

            from .ocr import get_provider
            provider = get_provider()

            if not register_job(collection, doc_id, total_pages,
                                mode='full', source='auto'):
                continue  # 手动 reocr 正在跑
            logger.info("自动 OCR 开始 coll=%s doc=%s (%s页): %s",
                        collection, doc_id, total_pages, row[0]['filename'])
            _run_reocr_doc(collection, doc_id, provider, pdf_path, total_pages)

            job = _reocr_get(collection, doc_id)
            if job and job.get('status') == 'error':
                key = (collection, doc_id)
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
                _ocr_attempts.pop((collection, doc_id), None)
        except Exception as e:
            pop_job(collection, doc_id)
            key = (collection, doc_id)
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
        queued = sum(1 for c, _ in _ocr_queue if c == collection)
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
