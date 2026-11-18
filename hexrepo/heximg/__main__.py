"""支持 ``python -m heximg`` 调用命令行。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
