"""小書房 桌面启动器 (Windows 便携版主入口, 也可开发模式直跑).

流程: 加载 .env → 单实例探测 → 找空闲端口 → 线程起 uvicorn → 开浏览器 → 系统托盘常驻.

- 绑定 XSF_HOST (默认 127.0.0.1, 回环不触发防火墙弹窗; 想局域网访问需自担风险改 0.0.0.0 并放行防火墙)
- 端口 XSF_PORT 起 (默认 8090), 被占自动顺延至 +9
- 单实例: 首选端口已有 HTTP 服务在响应 → 只开浏览器然后退出
"""

import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from .env import load_env

POLL_INTERVAL = 0.2
START_TIMEOUT = 20.0


def _log(msg: str) -> None:
    # 开发模式上屏; windowed exe 落 xsf-desktop.log (见 _ensure_streams)
    print(msg, file=sys.stderr, flush=True)


def _ensure_streams() -> None:
    """windowed exe (console=False) 的 stdout/stderr 是 None,
    uvicorn formatter 的 sys.stdout.isatty()/print/warnings 都会 AttributeError.

    重定向到 exe 同目录 xsf-desktop.log (不可写则 devnull), 兼作用户排障日志.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    sink = None
    if getattr(sys, 'frozen', False):
        try:
            sink = open(Path(sys.executable).parent / 'xsf-desktop.log',
                        'a', buffering=1, encoding='utf-8')
        except OSError:
            pass
    if sink is None:
        sink = open(os.devnull, 'w', encoding='utf-8')
    sys.stdout = sys.stdout or sink
    sys.stderr = sys.stderr or sink


def _alert(msg: str, title: str = '小書房') -> None:
    """Windows 无控制台时的错误弹窗 (其它平台退化为 stderr)."""
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, title, 0x10)
            return
        except Exception:
            pass
    _log(msg)


def _port_serving(port: int) -> bool:
    """端口上是否已有 HTTP 服务响应 (任意状态码都算, 含登录重定向)."""
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=1.5)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def _pick_port(preferred: int) -> int | None:
    for p in range(preferred, preferred + 10):
        if _bindable(p):
            return p
    return None


def _resource_dir() -> Path:
    """打包后资源目录 (PyInstaller _MEIPASS), 开发模式回退到 repo packaging/."""
    base = getattr(sys, '_MEIPASS', None)
    if base:
        return Path(base) / 'packaging'
    return Path(__file__).resolve().parent.parent / 'packaging'


def _load_icon_image():
    """托盘图标: 优先打包内 xsf.ico, 缺失时 PIL 现画一个简易书本."""
    from PIL import Image, ImageDraw
    ico = _resource_dir() / 'xsf.ico'
    try:
        return Image.open(ico)
    except Exception:
        pass
    img = Image.new('RGBA', (64, 64), (44, 74, 124, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([10, 12, 54, 52], radius=4, fill=(250, 246, 236, 255))
    d.rectangle([30, 16, 34, 48], fill=(44, 74, 124, 255))
    d.line([14, 20, 28, 20], fill=(44, 74, 124, 255), width=2)
    d.line([14, 26, 28, 26], fill=(44, 74, 124, 255), width=2)
    d.line([36, 20, 50, 20], fill=(44, 74, 124, 255), width=2)
    d.line([36, 26, 50, 26], fill=(44, 74, 124, 255), width=2)
    return img


def main() -> None:
    _ensure_streams()          # 必须最先: 后续 _log/uvicorn 都依赖流存在
    load_env()

    host = os.environ.get('XSF_HOST', '127.0.0.1').strip() or '127.0.0.1'
    try:
        preferred = int(os.environ.get('XSF_PORT', '8090'))
    except ValueError:
        preferred = 8090

    # 单实例: 首选端口活着 → 只开浏览器
    if host == '127.0.0.1' and _port_serving(preferred):
        _log(f'端口 {preferred} 已有服务, 直接打开浏览器')
        webbrowser.open(f'http://127.0.0.1:{preferred}/')
        return

    port = _pick_port(preferred) if host == '127.0.0.1' else preferred
    if port is None:
        _alert(f'端口 {preferred}-{preferred + 9} 都被占用, 无法启动。\n'
               f'可设置 XSF_PORT 环境变量换端口。')
        sys.exit(1)

    import uvicorn
    from .api import app

    config = uvicorn.Config(app, host=host, port=port, log_level='warning')
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True, name='xsf-server')
    t.start()

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if server.started:
            break
        if not t.is_alive():
            _alert('服务线程异常退出, 请检查数据目录权限后重试。')
            sys.exit(1)
        time.sleep(POLL_INTERVAL)
    else:
        _alert(f'服务启动超时 ({int(START_TIMEOUT)}s)。')
        sys.exit(1)

    url = f'http://{"127.0.0.1" if host in ("0.0.0.0", "") else host}:{port}/'
    _open_browser(url)

    _run_tray(server, url)


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass  # headless / 无默认浏览器不致命, 服务照常


def _run_tray(server, url: str) -> None:
    """托盘常驻; 托盘不可用 (无 pystray / 无显示后端) 时退化为阻塞等待."""
    icon = None
    try:
        import pystray

        def _open(_icon=None, _item=None):
            _open_browser(url)

        def _quit(_icon=None, _item=None):
            server.should_exit = True
            icon.stop()

        menu = pystray.Menu(
            pystray.MenuItem(f'打开小書房 ({url})', _open, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('退出', _quit),
        )
        icon = pystray.Icon('xsf', _load_icon_image(),
                            f'小書房 — {url}', menu)
    except Exception as exc:  # ImportError / Xlib DisplayNameError 等
        _log(f'托盘不可用 ({exc}), 服务运行于 {url}')

    if icon is None:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        return

    try:
        icon.run()
    except Exception as exc:
        _log(f'托盘异常退出 ({exc}), 服务保持运行于 {url} (Ctrl+C 退出)')
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    server.should_exit = True
    _log('bye')


if __name__ == '__main__':
    main()
