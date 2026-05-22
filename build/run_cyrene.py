"""PyInstaller 入口 — 默认以 Web 模式启动 Cyrene。"""
import sys
import webbrowser


def _open_browser(port: int) -> None:
    import threading
    import time
    def _delayed():
        time.sleep(1.5)
        webbrowser.open(f"http://localhost:{port}")
    t = threading.Thread(target=_delayed, daemon=True)
    t.start()


if __name__ == "__main__":
    # 确保 --web 在 argv 中
    if "--web" not in sys.argv:
        sys.argv.append("--web")

    from cyrene.config import WEB_PORT
    _open_browser(WEB_PORT)

    from cyrene.local_cli import main
    main()
