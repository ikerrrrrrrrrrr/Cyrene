"""PyInstaller 入口 — 原生桌面窗口模式启动 Cyrene。"""
import sys

if __name__ == "__main__":
    if "--gui" not in sys.argv:
        sys.argv.append("--gui")

    from cyrene.local_cli import main
    main()
