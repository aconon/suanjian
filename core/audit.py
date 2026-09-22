#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.audit — 审计：所有写操作留痕（照军机处 core/audit.py 模式移植）。

用法一行：audit(conn, actor, action, target, **detail)。
绝不抛错（审计失败不能砸业务），失败时 stderr 兜底（DB 全挂时终端是唯一必达通道）。
"""
import contextlib
import json
import logging


def audit(conn, actor: str, action: str, target: str = "", **detail) -> None:
    try:
        conn.execute(
            "INSERT INTO audit_log(actor,action,target,detail_json) VALUES(?,?,?,?)",
            (actor, action, target, json.dumps(detail, ensure_ascii=False, default=str)))
        conn.commit()
    except Exception as e:  # pragma: no cover - 兜底路径
        try:
            import sys
            sys.stderr.write(f"[audit-fallback] {actor} {action} {target} {detail} err={e}\n")
        except Exception:   # stderr 对象也写不进（极端）：os.write 直写 fd 2 是最后通道，仍不砸业务
            import os
            try:
                os.write(2, b"[audit-fallback] both stderr and db failed\n")
            except OSError:
                # fd 2 都关了：logging 框架自带 OSError 兜底（Handler.handleError 不外抛），
                # suppress 再保一层「审计失败绝不砸业务」铁律——留痕尝试到此为止
                with contextlib.suppress(Exception):
                    logging.exception("audit 兜底失败：db 与 stderr 都不可用")
