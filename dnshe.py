#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DNSHE 域名续期工具入口。

  python dnshe.py list      # 查询全部域名
  python dnshe.py auto      # 自动续期（计划任务调用）
  python dnshe.py web       # 本地管理面板
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dnshe.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
