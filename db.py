#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""absolute_term PostgreSQL 访问层。缺配置或连不上就报错，不静默降级。"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import configparser

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.ini"
SCHEMA_PATH = ROOT / "schema.sql"


class DbError(RuntimeError):
    pass


def _read_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if not CONFIG_PATH.is_file():
        raise DbError(f"缺少配置文件: {CONFIG_PATH}")
    cfg.read(str(CONFIG_PATH), encoding="utf-8")
    local = ROOT / "config.ini.local"
    if local.is_file():
        cfg.read(str(local), encoding="utf-8")
    return cfg


def db_settings() -> dict[str, Any]:
    cfg = _read_config()
    if not cfg.has_section("db"):
        raise DbError("config.ini 缺少 [db] 段")
    required = ("host", "port", "name", "user", "password", "password_salt")
    out: dict[str, Any] = {}
    for key in required:
        val = cfg.get("db", key, fallback="").strip()
        if not val:
            raise DbError(f"config.ini [db] {key} 未配置（密钥可放 config.ini.local）")
        out[key] = val
    out["port"] = int(out["port"])
    return out


def hash_password(password: str, salt: str | None = None) -> str:
    if not password:
        raise DbError("密码不能为空")
    if salt is None:
        salt = db_settings()["password_salt"]
    raw = f"{salt}:{password}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    return hash_password(password) == password_hash


@contextmanager
def connect() -> Iterator[Any]:
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError as e:
        raise DbError(f"服务器未安装 psycopg2: {e}") from e
    s = db_settings()
    try:
        conn = psycopg2.connect(
            host=s["host"],
            port=s["port"],
            dbname=s["name"],
            user=s["user"],
            password=s["password"],
        )
    except Exception as e:  # noqa: BLE001
        raise DbError(f"连接数据库失败: {e}") from e
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_columns(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


def migrate_legacy_shop_tb(cur) -> None:
    """把旧列名迁到 fix.md 结构：user_id/shop_link/goods_sum/tb_shop_id/bad_goods_sum。"""
    cols = _table_columns(cur, "shop_tb")
    if not cols:
        return
    renames = [
        ("owner_user_id", "user_id"),
        ("shop_url", "shop_link"),
        ("item_count", "goods_sum"),
        ("shop_id", "tb_shop_id"),
    ]
    for old, new in renames:
        if old in cols and new not in cols:
            cur.execute(f'ALTER TABLE shop_tb RENAME COLUMN "{old}" TO "{new}"')
            cols.discard(old)
            cols.add(new)
    if "bad_goods_sum" not in cols:
        cur.execute(
            "ALTER TABLE shop_tb ADD COLUMN bad_goods_sum integer NOT NULL DEFAULT 0"
        )
    if "shop_link" not in cols:
        cur.execute("ALTER TABLE shop_tb ADD COLUMN shop_link text NOT NULL DEFAULT ''")
    if "goods_sum" not in cols:
        cur.execute(
            "ALTER TABLE shop_tb ADD COLUMN goods_sum integer NOT NULL DEFAULT 0"
        )
    if "tb_shop_id" not in cols:
        cur.execute("ALTER TABLE shop_tb ADD COLUMN tb_shop_id text NOT NULL DEFAULT ''")
    if "user_id" not in cols and "owner_user_id" in cols:
        cur.execute('ALTER TABLE shop_tb RENAME COLUMN "owner_user_id" TO "user_id"')
    if "source_md" not in cols:
        cur.execute("ALTER TABLE shop_tb ADD COLUMN source_md text NOT NULL DEFAULT ''")


def migrate_user_tb(cur) -> None:
    """补 user_tb.balance / auto_scan / phone / last_login_*。"""
    cols = _table_columns(cur, "user_tb")
    if not cols:
        return
    if "balance" not in cols:
        cur.execute(
            "ALTER TABLE user_tb ADD COLUMN balance numeric(12,2) NOT NULL DEFAULT 0"
        )
    if "auto_scan" not in cols:
        cur.execute(
            "ALTER TABLE user_tb ADD COLUMN auto_scan boolean NOT NULL DEFAULT false"
        )
    if "phone" not in cols:
        cur.execute(
            "ALTER TABLE user_tb ADD COLUMN phone text NOT NULL DEFAULT ''"
        )
    if "last_login_ip" not in cols:
        cur.execute(
            "ALTER TABLE user_tb ADD COLUMN last_login_ip text NOT NULL DEFAULT ''"
        )
    if "last_login_city" not in cols:
        cur.execute(
            "ALTER TABLE user_tb ADD COLUMN last_login_city text NOT NULL DEFAULT ''"
        )
    if "last_login_at" not in cols:
        cur.execute("ALTER TABLE user_tb ADD COLUMN last_login_at timestamptz")


def cleanup_goods_tb(cur) -> None:
    """goods_tb 只留有问题的商品：历史「没问题」的行硬删，并刷新各店计数。

    goods_sum 保持历史总数不重算（删除前它就等于全部商品数）。
    """
    cur.execute("DELETE FROM goods_tb WHERE coalesce(problem, '') = ''")
    cur.execute(
        """
        UPDATE shop_tb s SET
            bad_goods_sum = (
                SELECT count(*) FROM goods_tb g WHERE g.shop_id = s.id
            ),
            goods_sum = GREATEST(
                s.goods_sum,
                COALESCE(jsonb_array_length(s.item_ids), 0),
                (SELECT count(*) FROM goods_tb g WHERE g.shop_id = s.id)
            )
        """
    )


def migrate_session_tb(cur) -> None:
    """旧 session_tb（无 channel）直接重建；当前代码此前用内存会话，旧行无用。"""
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'session_tb'
        """
    )
    cols = {r[0] for r in cur.fetchall()}
    if not cols:
        return
    if "channel" in cols and "last_seen_at" in cols:
        return
    cur.execute("DROP TABLE IF EXISTS session_tb CASCADE")


def init_schema() -> None:
    if not SCHEMA_PATH.is_file():
        raise DbError(f"缺少 schema.sql: {SCHEMA_PATH}")
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect() as conn:
        with conn.cursor() as cur:
            # 先迁旧表，再执行 CREATE IF NOT EXISTS（含 goods_tb）
            migrate_legacy_shop_tb(cur)
            migrate_user_tb(cur)
            migrate_session_tb(cur)
            cur.execute(sql)
            migrate_legacy_shop_tb(cur)
            migrate_user_tb(cur)
            migrate_session_tb(cur)
            # goods_tb 只留问题商品；顺带清掉历史「没问题」的存量行
            cleanup_goods_tb(cur)


def create_session_row(
    user_id: int,
    token: str,
    *,
    channel: str = "web",
    ttl_seconds: int | None = None,
) -> None:
    """写入 session_tb。channel=client 时 expires_at 为空（不过期）；web 必须给 ttl。"""
    token = str(token or "").strip()
    channel = str(channel or "").strip().lower()
    if not token:
        raise DbError("token 不能为空")
    if channel not in ("client", "web"):
        raise DbError(f"未知会话 channel: {channel}")
    if channel == "web":
        if ttl_seconds is None or int(ttl_seconds) <= 0:
            raise DbError("网页会话须配置正数 ttl_seconds")
        ttl = int(ttl_seconds)
        expires_sql = "now() + (%s || ' seconds')::interval"
        expires_arg: tuple[Any, ...] = (str(ttl),)
    else:
        expires_sql = "NULL"
        expires_arg = ()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO session_tb (token, user_id, channel, expires_at, last_seen_at)
                VALUES (%s, %s, %s, {expires_sql}, now())
                ON CONFLICT (token) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    channel = EXCLUDED.channel,
                    expires_at = EXCLUDED.expires_at,
                    last_seen_at = now()
                """,
                (token, int(user_id), channel, *expires_arg),
            )


def get_session_row(token: str) -> dict | None:
    """有效会话；过期行硬删除后返回 None。client 且 expires_at 为空视为永不过期。"""
    token = str(token or "").strip()
    if not token:
        return None
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT token, user_id, channel, expires_at
                FROM session_tb WHERE token = %s
                """,
                (token,),
            )
            row = cur.fetchone()
            if not row:
                return None
            tok, uid, channel, exp = row
            channel = str(channel or "web")
            if exp is not None:
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp < datetime.now(timezone.utc):
                    cur.execute("DELETE FROM session_tb WHERE token = %s", (token,))
                    return None
            elif channel != "client":
                # web 不允许空过期
                cur.execute("DELETE FROM session_tb WHERE token = %s", (token,))
                return None
            cur.execute(
                "UPDATE session_tb SET last_seen_at = now() WHERE token = %s",
                (token,),
            )
    return {
        "token": str(tok),
        "user_id": int(uid),
        "channel": channel,
        "expires_at": exp,
    }


def delete_session_row(token: str) -> None:
    token = str(token or "").strip()
    if not token:
        return
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM session_tb WHERE token = %s", (token,))


def delete_sessions_for_user(user_id: int, *, channel: str | None = None) -> int:
    """改密等场景作废会话。channel 为空则删该用户全部。"""
    with connect() as conn:
        with conn.cursor() as cur:
            if channel:
                cur.execute(
                    "DELETE FROM session_tb WHERE user_id = %s AND channel = %s",
                    (int(user_id), str(channel)),
                )
            else:
                cur.execute(
                    "DELETE FROM session_tb WHERE user_id = %s",
                    (int(user_id),),
                )
            return int(cur.rowcount or 0)


def ensure_test_user(username: str = "", password: str = "") -> dict:
    """确保测试用户存在；已存在则不改密码，只返回信息。用户名/密码不能为空。"""
    if not username or not password:
        raise DbError("测试用户名/密码不能为空")
    ph = hash_password(password)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, enable FROM user_tb WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
            if row:
                return {"id": row[0], "username": row[1], "enable": row[2], "created": False}
            cur.execute(
                """
                INSERT INTO user_tb (username, password_hash, cookie)
                VALUES (%s, %s, '')
                RETURNING id, username, enable
                """,
                (username, ph),
            )
            row = cur.fetchone()
            return {"id": row[0], "username": row[1], "enable": row[2], "created": True}


def get_user_by_username(username: str) -> dict | None:
    username = (username or "").strip()
    if not username:
        raise DbError("username 不能为空")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, password_hash, cookie, enable,
                       created_at, updated_at, coalesce(balance, 0),
                       coalesce(auto_scan, false), coalesce(phone, ''),
                       coalesce(last_login_ip, ''), coalesce(last_login_city, ''),
                       last_login_at
                FROM user_tb WHERE username = %s
                """,
                (username,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "username": row[1],
        "password_hash": row[2],
        "cookie": row[3] or "",
        "enable": row[4],
        "created_at": row[5],
        "updated_at": row[6],
        "balance": float(row[7] or 0),
        "auto_scan": bool(row[8]),
        "phone": row[9] or "",
        "last_login_ip": row[10] or "",
        "last_login_city": row[11] or "",
        "last_login_at": row[12],
    }


def get_user_by_id(user_id: int) -> dict | None:
    if not user_id:
        raise DbError("user_id 无效")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, password_hash, cookie, enable,
                       created_at, updated_at, coalesce(balance, 0),
                       coalesce(auto_scan, false), coalesce(phone, ''),
                       coalesce(last_login_ip, ''), coalesce(last_login_city, ''),
                       last_login_at
                FROM user_tb WHERE id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "username": row[1],
        "password_hash": row[2],
        "cookie": row[3] or "",
        "enable": row[4],
        "created_at": row[5],
        "updated_at": row[6],
        "balance": float(row[7] or 0),
        "auto_scan": bool(row[8]),
        "phone": row[9] or "",
        "last_login_ip": row[10] or "",
        "last_login_city": row[11] or "",
        "last_login_at": row[12],
    }


def record_user_login(user_id: int, ip: str, city: str = "") -> None:
    """记录用户最近一次登录的 IP 与城市；IP 为空则拒绝（不猜数据）。"""
    if not user_id:
        raise DbError("user_id 无效")
    ip = str(ip or "").strip()
    if not ip:
        raise DbError("登录 IP 为空，无法记录")
    city = str(city or "").strip()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE user_tb
                SET last_login_ip = %s,
                    last_login_city = %s,
                    last_login_at = now(),
                    updated_at = now()
                WHERE id = %s
                """,
                (ip, city, int(user_id)),
            )
            if cur.rowcount == 0:
                raise DbError(f"用户不存在: id={user_id}")


def authenticate(username: str, password: str) -> dict:
    user = get_user_by_username(username)
    if not user:
        raise DbError("用户名或密码错误")
    if user.get("enable") != "T":
        raise DbError("用户已禁用")
    if not verify_password(password, user["password_hash"]):
        raise DbError("用户名或密码错误")
    return {
        "id": user["id"],
        "username": user["username"],
        "has_cookie": bool((user.get("cookie") or "").strip()),
        "balance": float(user.get("balance") or 0),
        "auto_scan": bool(user.get("auto_scan")),
        "phone": user.get("phone") or "",
    }


def _norm_phone(phone: str) -> str:
    raw = str(phone or "").strip().replace("+86", "")
    if raw.startswith("86") and len(raw) == 13:
        raw = raw[2:]
    return raw


def create_user(username: str, password: str, phone: str = "") -> dict:
    username = (username or "").strip()
    if not username:
        raise DbError("用户名不能为空")
    if len(username) < 2 or len(username) > 32:
        raise DbError("用户名长度须 2～32")
    if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]+", username):
        raise DbError("用户名只能含字母、数字、下划线或中文")
    if not password:
        raise DbError("密码不能为空")
    if len(password) < 6:
        raise DbError("密码至少 6 位")
    phone = _norm_phone(phone)
    if phone and not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise DbError("手机号不正确")
    if get_user_by_username(username):
        raise DbError("用户名已存在")
    ph = hash_password(password)
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_tb (username, password_hash, cookie, phone)
                    VALUES (%s, %s, '', %s)
                    RETURNING id
                    """,
                    (username, ph, phone),
                )
                uid = int(cur.fetchone()[0])
    except Exception as e:
        msg = str(e).lower()
        if "unique" in msg or "duplicate" in msg:
            raise DbError("用户名已存在") from e
        raise
    user = get_user_by_id(uid)
    if not user:
        raise DbError("注册失败：写入后读不到用户")
    return user


def set_user_password(user_id: int, password: str) -> None:
    if not user_id:
        raise DbError("user_id 无效")
    if not password:
        raise DbError("密码不能为空")
    if len(password) < 6:
        raise DbError("密码至少 6 位")
    ph = hash_password(password)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_tb SET password_hash = %s, updated_at = now() WHERE id = %s",
                (ph, int(user_id)),
            )
            if cur.rowcount != 1:
                raise DbError("用户不存在")
    # 改密后旧 token 全部作废（含客户端）
    delete_sessions_for_user(int(user_id))


def set_user_phone(user_id: int, phone: str) -> dict:
    if not user_id:
        raise DbError("user_id 无效")
    phone = _norm_phone(phone)
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise DbError("手机号不正确")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_tb SET phone = %s, updated_at = now() WHERE id = %s",
                (phone, int(user_id)),
            )
            if cur.rowcount != 1:
                raise DbError("用户不存在")
    user = get_user_by_id(int(user_id))
    if not user:
        raise DbError("用户不存在")
    return user


def rename_user(user_id: int, new_username: str) -> dict:
    """改用户名：格式+唯一性校验；成功返回新 user。"""
    if not user_id:
        raise DbError("user_id 无效")
    username = (new_username or "").strip()
    if not username:
        raise DbError("用户名不能为空")
    if len(username) < 2 or len(username) > 32:
        raise DbError("用户名长度须 2～32")
    if not re.fullmatch(r"[A-Za-z0-9_一-鿿]+", username):
        raise DbError("用户名只能含字母、数字、下划线或中文")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM user_tb WHERE username = %s", (username,))
            row = cur.fetchone()
            if row and int(row[0]) != int(user_id):
                raise DbError("用户名已存在")
            cur.execute(
                "UPDATE user_tb SET username = %s, updated_at = now() WHERE id = %s",
                (username, int(user_id)),
            )
            if cur.rowcount != 1:
                raise DbError("用户不存在")
    user = get_user_by_id(int(user_id))
    if not user:
        raise DbError("用户不存在")
    return user


def list_usernames_by_phone(phone: str) -> list[str]:
    phone = _norm_phone(phone)
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise DbError("手机号不正确")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username FROM user_tb WHERE phone = %s ORDER BY id ASC",
                (phone,),
            )
            rows = cur.fetchall()
    return [str(r[0]) for r in rows]


def save_sms_code(phone: str, purpose: str, code: str, ttl_seconds: int, resend_seconds: int) -> None:
    phone = _norm_phone(phone)
    purpose = str(purpose or "").strip()
    code = str(code or "").strip()
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise DbError("手机号不正确")
    if purpose not in ("reset", "recover", "bind"):
        raise DbError(f"未知短信用途: {purpose}")
    if not re.fullmatch(r"\d{4,8}", code):
        raise DbError("验证码格式无效")
    try:
        ttl = int(ttl_seconds)
        wait = int(resend_seconds)
    except (TypeError, ValueError) as e:
        raise DbError("短信时间参数无效") from e
    if ttl < 30:
        raise DbError("验证码有效期过短")
    if wait < 0:
        raise DbError("重发间隔不能为负")
    with connect() as conn:
        with conn.cursor() as cur:
            if wait > 0:
                cur.execute(
                    """
                    SELECT created_at FROM sms_code_tb
                    WHERE phone = %s AND purpose = %s
                    ORDER BY id DESC LIMIT 1
                    """,
                    (phone, purpose),
                )
                row = cur.fetchone()
                if row and row[0]:
                    created = row[0]
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - created < timedelta(seconds=wait):
                        raise DbError(f"请 {wait} 秒后再发验证码")
            cur.execute(
                "DELETE FROM sms_code_tb WHERE phone = %s AND purpose = %s",
                (phone, purpose),
            )
            cur.execute(
                """
                INSERT INTO sms_code_tb (phone, purpose, code, expires_at)
                VALUES (%s, %s, %s, now() + (%s || ' seconds')::interval)
                """,
                (phone, purpose, code, str(ttl)),
            )


def consume_sms_code(phone: str, purpose: str, code: str) -> None:
    phone = _norm_phone(phone)
    purpose = str(purpose or "").strip()
    code = str(code or "").strip()
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise DbError("手机号不正确")
    if not code:
        raise DbError("请填写验证码")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM sms_code_tb
                WHERE phone = %s AND purpose = %s AND code = %s AND expires_at > now()
                """,
                (phone, purpose, code),
            )
            n = cur.rowcount
    if n < 1:
        raise DbError("验证码错误或已过期")


def delete_sms_codes(phone: str, purpose: str) -> None:
    phone = _norm_phone(phone)
    purpose = str(purpose or "").strip()
    if not phone or not purpose:
        return
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sms_code_tb WHERE phone = %s AND purpose = %s",
                (phone, purpose),
            )


def save_user_cookie(user_id: int, cookie: str) -> dict:
    if not user_id:
        raise DbError("user_id 无效")
    cookie = (cookie or "").strip()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE user_tb
                SET cookie = %s, updated_at = now()
                WHERE id = %s
                RETURNING id, username, length(cookie)
                """,
                (cookie, user_id),
            )
            row = cur.fetchone()
    if not row:
        raise DbError(f"用户不存在: id={user_id}")
    return {"ok": True, "id": row[0], "username": row[1], "cookie_len": row[2]}


def change_balance(user_id: int, delta_yuan: float) -> float:
    """加减余额（元）。不足则报错。返回新余额。"""
    if not user_id:
        raise DbError("user_id 无效")
    try:
        delta = float(delta_yuan)
    except (TypeError, ValueError) as e:
        raise DbError(f"金额无效: {delta_yuan!r}") from e
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT coalesce(balance, 0) FROM user_tb WHERE id = %s FOR UPDATE",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                raise DbError(f"用户不存在: id={user_id}")
            old = float(row[0] or 0)
            new = round(old + delta, 2)
            if new < 0:
                raise DbError(f"余额不足（当前 {old:.2f} 元）")
            cur.execute(
                """
                UPDATE user_tb SET balance = %s, updated_at = now()
                WHERE id = %s
                """,
                (new, user_id),
            )
    return new


def enable_auto_scan(user_id: int, fee_yuan: float) -> dict:
    """永久开通自动扫描。已开通不重复扣费。余额不足报错。"""
    if not user_id:
        raise DbError("user_id 无效")
    try:
        fee = float(fee_yuan)
    except (TypeError, ValueError) as e:
        raise DbError(f"自动扫描费用无效: {fee_yuan!r}") from e
    if fee < 0:
        raise DbError("自动扫描费用不能为负")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT coalesce(auto_scan, false), coalesce(balance, 0)
                FROM user_tb WHERE id = %s FOR UPDATE
                """,
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                raise DbError(f"用户不存在: id={user_id}")
            already = bool(row[0])
            bal = float(row[1] or 0)
            if already:
                return {
                    "ok": True,
                    "auto_scan": True,
                    "charged": False,
                    "balance": bal,
                    "fee_yuan": fee,
                }
            if bal < fee:
                raise DbError(
                    f"余额不足：开通自动扫描需 {fee:.2f} 元，当前 {bal:.2f} 元，请先充值"
                )
            new_bal = round(bal - fee, 2)
            cur.execute(
                """
                UPDATE user_tb
                SET auto_scan = true, balance = %s, updated_at = now()
                WHERE id = %s
                """,
                (new_bal, user_id),
            )
    return {
        "ok": True,
        "auto_scan": True,
        "charged": True,
        "balance": new_bal,
        "fee_yuan": fee,
    }


def _scan_shop_row(row: tuple) -> dict:
    return {
        "id": row[0],
        "user_id": row[1],
        "shop_name": row[2] or "",
        "shop_link": row[3] or "",
        "tb_shop_id": row[4] or "",
        "seller_id": row[5] or "",
        "item_id": row[6] or "",
        "created_at": _ts(row[7]) if len(row) > 7 else 0,
    }


def list_scan_shops(owner_user_id: int) -> list[dict]:
    if not owner_user_id:
        raise DbError("user_id 无效")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, shop_name, shop_link, tb_shop_id, seller_id,
                       item_id, created_at
                FROM scan_shop_tb
                WHERE user_id = %s
                ORDER BY id ASC
                """,
                (owner_user_id,),
            )
            rows = cur.fetchall()
    return [_scan_shop_row(r) for r in rows]


def shop_already_in_db(owner_user_id: int, tb_shop_id: str) -> bool:
    """本用户 shop_tb 或 scan_shop_tb 是否已有该淘宝店。"""
    sid = str(tb_shop_id or "").strip()
    if not owner_user_id or not sid:
        raise DbError("user_id / tb_shop_id 无效")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM shop_tb WHERE user_id = %s AND tb_shop_id = %s LIMIT 1",
                (owner_user_id, sid),
            )
            if cur.fetchone():
                return True
            cur.execute(
                "SELECT 1 FROM scan_shop_tb WHERE user_id = %s AND tb_shop_id = %s LIMIT 1",
                (owner_user_id, sid),
            )
            return bool(cur.fetchone())


def add_scan_shop(
    owner_user_id: int,
    *,
    shop_name: str,
    shop_link: str,
    tb_shop_id: str,
    seller_id: str = "",
    item_id: str = "",
) -> dict:
    if not owner_user_id:
        raise DbError("user_id 无效")
    tb_shop_id = str(tb_shop_id or "").strip()
    shop_link = str(shop_link or "").strip()
    shop_name = str(shop_name or "").strip()
    seller_id = str(seller_id or "").strip()
    item_id = str(item_id or "").strip()
    if not tb_shop_id:
        raise DbError("tb_shop_id 不能为空")
    if not shop_link:
        raise DbError("shop_link 不能为空")
    if shop_already_in_db(owner_user_id, tb_shop_id):
        return {
            "ok": True,
            "added": False,
            "skipped": True,
            "reason": "店铺已在 shop_tb 或 scan_shop_tb",
            "tb_shop_id": tb_shop_id,
        }
    name = shop_name or tb_shop_id
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scan_shop_tb (
                    user_id, shop_name, shop_link, tb_shop_id, seller_id, item_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, user_id, shop_name, shop_link, tb_shop_id, seller_id,
                          item_id, created_at
                """,
                (owner_user_id, name, shop_link, tb_shop_id, seller_id, item_id),
            )
            row = cur.fetchone()
    return {"ok": True, "added": True, "skipped": False, "shop": _scan_shop_row(row)}


def delete_scan_shop(owner_user_id: int, *, pk: int = 0, tb_shop_id: str = "") -> dict:
    """硬删除待检查记录。"""
    if not owner_user_id:
        raise DbError("user_id 无效")
    sid = str(tb_shop_id or "").strip()
    if not pk and not sid:
        raise DbError("删除待检查店铺需要 id 或 tb_shop_id")
    with connect() as conn:
        with conn.cursor() as cur:
            if pk:
                cur.execute(
                    "DELETE FROM scan_shop_tb WHERE user_id = %s AND id = %s",
                    (owner_user_id, int(pk)),
                )
            else:
                cur.execute(
                    "DELETE FROM scan_shop_tb WHERE user_id = %s AND tb_shop_id = %s",
                    (owner_user_id, sid),
                )
            n = cur.rowcount
    return {"ok": True, "deleted": n}


def _shop_row(row: tuple) -> dict:
    """row: id,user_id,shop_name,shop_link,goods_sum,bad_goods_sum,tb_shop_id,seller_id,
    item_ids,item_titles,status,last_error,created_at,updated_at
    """
    item_ids = row[8]
    item_titles = row[9]
    if isinstance(item_ids, str):
        item_ids = json.loads(item_ids)
    if isinstance(item_titles, str):
        item_titles = json.loads(item_titles)
    tb_shop_id = row[6] or ""
    return {
        "id": row[0],
        "user_id": row[1],  # 系统用户 FK
        "owner_user_id": row[1],
        "shop_name": row[2] or tb_shop_id,
        "shop_link": row[3] or "",
        "shop_url": row[3] or "",
        "goods_sum": int(row[4] or 0),
        "bad_goods_sum": int(row[5] or 0),
        "problem_link_count": int(row[5] or 0),  # 菜单「问题链接数」
        "tb_shop_id": tb_shop_id,
        "shop_id": tb_shop_id,  # 兼容旧字段（淘宝 shopId）
        "seller_id": row[7] or "",
        "item_ids": list(item_ids or []),
        "item_titles": dict(item_titles or {}),
        "item_count": int(row[4] or 0),
        "status": row[10],
        "last_error": row[11] or "",
        "created_at": _ts(row[12]),
        "updated_at": _ts(row[13]),
        "has_source_md": bool(row[14]) if len(row) > 14 else False,
    }


def _ts(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.timestamp()
    return float(v)


_SHOP_COLS = """
id, user_id, shop_name, shop_link, goods_sum, bad_goods_sum, tb_shop_id, seller_id,
item_ids, item_titles, status, last_error, created_at, updated_at
"""
_SHOP_LIST_COLS = (
    _SHOP_COLS.strip() + ", (coalesce(source_md, '') <> '') AS has_source_md"
)


def list_shops(owner_user_id: int) -> list[dict]:
    if not owner_user_id:
        raise DbError("user_id 无效")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_SHOP_LIST_COLS}
                FROM shop_tb
                WHERE user_id = %s
                ORDER BY updated_at DESC, id DESC
                """,
                (owner_user_id,),
            )
            rows = cur.fetchall()
    return [_shop_row(r) for r in rows]


def get_shop(owner_user_id: int, shop_id: str) -> dict | None:
    """shop_id 此处为淘宝 tb_shop_id。"""
    shop_id = str(shop_id or "").strip()
    if not owner_user_id or not shop_id:
        raise DbError("user_id / tb_shop_id 无效")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_SHOP_LIST_COLS}
                FROM shop_tb
                WHERE user_id = %s AND tb_shop_id = %s
                """,
                (owner_user_id, shop_id),
            )
            row = cur.fetchone()
    return _shop_row(row) if row else None


def get_shop_by_name(owner_user_id: int, shop_name: str) -> dict | None:
    """按店名找本用户店铺。优先尚未写入 source_md 的，便于旧本地文件回传。"""
    name = str(shop_name or "").strip()
    if not owner_user_id or not name:
        raise DbError("user_id / shop_name 无效")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_SHOP_LIST_COLS}
                FROM shop_tb
                WHERE user_id = %s AND shop_name = %s
                ORDER BY (coalesce(source_md, '') = '') DESC, updated_at DESC, id DESC
                LIMIT 1
                """,
                (int(owner_user_id), name),
            )
            row = cur.fetchone()
    return _shop_row(row) if row else None


def upsert_shop_db(
    owner_user_id: int,
    shop_id: str,
    seller_id: str = "",
    shop_name: str = "",
    shop_url: str = "",
    item_ids: list | None = None,
    item_titles: dict | None = None,
    status: str = "ready",
    last_error: str = "",
    bad_goods_sum: int | None = None,
) -> dict:
    tb_shop_id = str(shop_id or "").strip()
    seller_id = str(seller_id or "").strip()
    if not owner_user_id:
        raise DbError("user_id 无效")
    if not tb_shop_id:
        raise DbError("tb_shop_id 不能为空")
    if not seller_id:
        raise DbError("seller_id（淘宝卖家 userId）不能为空")
    ids = [str(x) for x in (item_ids or []) if str(x).strip()]
    titles = {str(k): str(v) for k, v in (item_titles or {}).items() if str(k).strip()}
    name = (shop_name or "").strip() or tb_shop_id
    link = (shop_url or "").strip()
    goods_sum = len(ids) if ids else 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO shop_tb (
                    user_id, tb_shop_id, seller_id, shop_name, shop_link,
                    item_ids, item_titles, goods_sum, bad_goods_sum,
                    status, last_error, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s,
                    %s, %s, now()
                )
                ON CONFLICT (user_id, tb_shop_id) DO UPDATE SET
                    seller_id = EXCLUDED.seller_id,
                    shop_name = CASE
                        WHEN EXCLUDED.shop_name <> '' THEN EXCLUDED.shop_name
                        ELSE shop_tb.shop_name END,
                    shop_link = CASE
                        WHEN EXCLUDED.shop_link <> '' THEN EXCLUDED.shop_link
                        ELSE shop_tb.shop_link END,
                    item_ids = CASE
                        WHEN jsonb_array_length(EXCLUDED.item_ids) > 0 THEN EXCLUDED.item_ids
                        ELSE shop_tb.item_ids END,
                    item_titles = CASE
                        WHEN EXCLUDED.item_titles <> '{{}}'::jsonb THEN EXCLUDED.item_titles
                        ELSE shop_tb.item_titles END,
                    goods_sum = CASE
                        WHEN EXCLUDED.goods_sum > 0 THEN EXCLUDED.goods_sum
                        ELSE shop_tb.goods_sum END,
                    bad_goods_sum = COALESCE(%s, shop_tb.bad_goods_sum),
                    status = EXCLUDED.status,
                    last_error = EXCLUDED.last_error,
                    updated_at = now()
                RETURNING {_SHOP_COLS}
                """,
                (
                    owner_user_id, tb_shop_id, seller_id, name, link,
                    json.dumps(ids, ensure_ascii=False),
                    json.dumps(titles, ensure_ascii=False),
                    goods_sum,
                    int(bad_goods_sum or 0),
                    status or "ready",
                    last_error or "",
                    bad_goods_sum,
                ),
            )
            row = cur.fetchone()
    return _shop_row(row)


def delete_shop_db(owner_user_id: int, shop_id: str) -> dict:
    shop_id = str(shop_id or "").strip()
    if not owner_user_id or not shop_id:
        raise DbError("user_id / tb_shop_id 无效")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM shop_tb WHERE user_id = %s AND tb_shop_id = %s",
                (owner_user_id, shop_id),
            )
            n = cur.rowcount
    return {"ok": True, "deleted": n}


def refresh_shop_counts(
    shop_pk: int,
    *,
    item_ids: list[str] | None = None,
    item_titles: dict | None = None,
    shop_name: str = "",
    shop_link: str = "",
    goods_sum: int | None = None,
) -> dict:
    """刷新店铺计数。goods_tb 只存问题商品，bad_goods_sum 按实数算；
    goods_sum（总商品数）只能由扫描方上报（给多少写多少），不再按 goods_tb 行数猜。
    """
    if not shop_pk:
        raise DbError("shop_id 无效")
    name = (shop_name or "").strip()
    link = (shop_link or "").strip()
    ids_json = json.dumps(item_ids, ensure_ascii=False) if item_ids is not None else None
    titles_json = (
        json.dumps(item_titles, ensure_ascii=False) if item_titles is not None else None
    )
    if goods_sum is not None and int(goods_sum) < 0:
        raise DbError("goods_sum 不能为负")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE shop_tb SET
                    goods_sum = CASE WHEN %s::integer IS NOT NULL THEN %s::integer
                                     ELSE shop_tb.goods_sum END,
                    bad_goods_sum = (
                        SELECT count(*) FROM goods_tb
                        WHERE shop_id = %s AND coalesce(problem, '') <> ''
                    ),
                    item_ids = CASE WHEN %s IS NOT NULL THEN %s::jsonb ELSE shop_tb.item_ids END,
                    item_titles = CASE WHEN %s IS NOT NULL THEN %s::jsonb ELSE shop_tb.item_titles END,
                    shop_name = CASE WHEN %s <> '' THEN %s ELSE shop_tb.shop_name END,
                    shop_link = CASE WHEN %s <> '' THEN %s ELSE shop_tb.shop_link END,
                    updated_at = now()
                WHERE id = %s
                RETURNING {_SHOP_COLS}
                """,
                (
                    goods_sum,
                    goods_sum,
                    shop_pk,
                    ids_json,
                    ids_json,
                    titles_json,
                    titles_json,
                    name,
                    name,
                    link,
                    link,
                    shop_pk,
                ),
            )
            row = cur.fetchone()
    if not row:
        raise DbError(f"店铺不存在: id={shop_pk}")
    return _shop_row(row)


def upsert_goods_db(
    shop_pk: int,
    tb_item_id: str,
    goods_name: str = "",
    goods_link: str = "",
    problem: str = "",
) -> dict:
    """写入「有问题」商品。problem 为空直接报错——goods_tb 不收没问题的商品。"""
    tb_item_id = str(tb_item_id or "").strip()
    if not shop_pk:
        raise DbError("shop_id 无效")
    if not tb_item_id:
        raise DbError("tb_item_id 不能为空")
    if not str(problem or "").strip():
        raise DbError(f"problem 为空的商品不写入 goods_tb: {tb_item_id}")
    link = (goods_link or "").strip() or f"https://item.taobao.com/item.htm?id={tb_item_id}"
    name = (goods_name or "").strip() or tb_item_id
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO goods_tb (shop_id, tb_item_id, goods_name, goods_link, problem, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (shop_id, tb_item_id) DO UPDATE SET
                    goods_name = CASE WHEN EXCLUDED.goods_name <> '' THEN EXCLUDED.goods_name
                                      ELSE goods_tb.goods_name END,
                    goods_link = CASE WHEN EXCLUDED.goods_link <> '' THEN EXCLUDED.goods_link
                                      ELSE goods_tb.goods_link END,
                    problem = EXCLUDED.problem,
                    updated_at = now()
                RETURNING id, shop_id, goods_name, goods_link, problem, tb_item_id
                """,
                (shop_pk, tb_item_id, name, link, problem or ""),
            )
            row = cur.fetchone()
            cur.execute(
                """
                UPDATE shop_tb SET
                    bad_goods_sum = (
                        SELECT count(*) FROM goods_tb
                        WHERE shop_id = %s AND coalesce(problem, '') <> ''
                    ),
                    updated_at = now()
                WHERE id = %s
                """,
                (shop_pk, shop_pk),
            )
    return {
        "id": row[0],
        "shop_id": row[1],
        "goods_name": row[2],
        "goods_link": row[3],
        "problem": row[4],
        "tb_item_id": row[5],
    }


def delete_goods_by_item_ids(shop_pk: int, item_ids: list[str]) -> int:
    """按商品 id 硬删 goods_tb 行（重扫后「以前有问题、现在没问题」的商品出表）。"""
    if not shop_pk:
        raise DbError("shop_id 无效")
    ids = [str(x).strip() for x in (item_ids or []) if str(x).strip()]
    if not ids:
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM goods_tb WHERE shop_id = %s AND tb_item_id = ANY(%s)",
                (int(shop_pk), ids),
            )
            return cur.rowcount


# ---------- 充值订单 ----------

def create_pay_order(user_id: int, order_no: str, channel: str, amount_cents: int) -> dict:
    if not user_id:
        raise DbError("user_id 无效")
    order_no = str(order_no or "").strip()
    if not order_no:
        raise DbError("order_no 不能为空")
    if channel not in ("wechat", "alipay"):
        raise DbError(f"channel 无效: {channel}")
    if int(amount_cents) <= 0:
        raise DbError("amount_cents 必须大于 0")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pay_order_tb (user_id, order_no, channel, amount_cents)
                VALUES (%s, %s, %s, %s)
                RETURNING id, user_id, order_no, channel, amount_cents, status
                """,
                (int(user_id), order_no, channel, int(amount_cents)),
            )
            r = cur.fetchone()
    return {
        "id": r[0], "user_id": r[1], "order_no": r[2],
        "channel": r[3], "amount_cents": r[4], "status": r[5],
    }


def get_pay_order(order_no: str) -> dict | None:
    order_no = str(order_no or "").strip()
    if not order_no:
        return None
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, order_no, channel, amount_cents, status, trade_no
                FROM pay_order_tb WHERE order_no = %s
                """,
                (order_no,),
            )
            r = cur.fetchone()
    if not r:
        return None
    return {
        "id": r[0], "user_id": r[1], "order_no": r[2], "channel": r[3],
        "amount_cents": r[4], "status": r[5], "trade_no": r[6] or "",
    }


def mark_pay_order_paid(order_no: str, trade_no: str = "") -> dict | None:
    """幂等入账：仅当 status=pending 时置 paid 并加余额；重复回调返回 None 不重复加钱。"""
    order_no = str(order_no or "").strip()
    if not order_no:
        return None
    yuan = 0.0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pay_order_tb
                SET status = 'paid', trade_no = COALESCE(NULLIF(%s, ''), trade_no),
                    paid_at = now()
                WHERE order_no = %s AND status = 'pending'
                RETURNING user_id, amount_cents, status
                """,
                (str(trade_no or ""), order_no),
            )
            r = cur.fetchone()
            if not r:
                return None  # 已 paid / closed / 不存在：不重复入账
            user_id, amount_cents = int(r[0]), int(r[1])
            yuan = round(amount_cents / 100.0, 2)
            cur.execute(
                "UPDATE user_tb SET balance = coalesce(balance,0) + %s, updated_at = now() "
                "WHERE id = %s",
                (yuan, user_id),
            )
    return {"order_no": order_no, "user_id": user_id, "yuan": yuan, "status": "paid"}


# ---- 用户意见/建议（suggestion_tb）----

SUGGESTION_MAX_LEN = 2000


def add_suggestion(user_id: int, comment: str) -> dict:
    """写入一条用户意见；空内容/超长直接报错。"""
    comment = str(comment or "").strip()
    if not comment:
        raise DbError("意见内容不能为空")
    if len(comment) > SUGGESTION_MAX_LEN:
        raise DbError(f"意见内容过长（最多 {SUGGESTION_MAX_LEN} 字）")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO suggestion_tb (comment, user_id) VALUES (%s, %s) "
                "RETURNING id, date",
                (comment, int(user_id)),
            )
            r = cur.fetchone()
        conn.commit()
    return {"id": r[0], "date": _ts(r[1])}


def list_suggestions(limit: int = 500) -> list[dict]:
    """列出意见（新→旧），带提交者用户名；用户已删则用户名留空。"""
    limit = int(limit or 500)
    if limit <= 0:
        raise DbError("limit 无效")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.comment, s.user_id, s.date, u.username
                FROM suggestion_tb s
                LEFT JOIN user_tb u ON u.id = s.user_id
                ORDER BY s.date DESC, s.id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "comment": r[1] or "",
            "user_id": r[2],
            "date": _ts(r[3]),
            "username": r[4] or "",
        }
        for r in rows
    ]


def delete_suggestion(suggestion_id: int) -> bool:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM suggestion_tb WHERE id = %s", (int(suggestion_id),))
            n = cur.rowcount
        conn.commit()
    return n > 0


def delete_suggestions(ids: list[int]) -> int:
    """批量删除；返回实际删除条数。空列表返回 0。"""
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM suggestion_tb WHERE id = ANY(%s)", (ids,))
            n = cur.rowcount
        conn.commit()
    return n


def list_goods(shop_pk: int) -> list[dict]:
    if not shop_pk:
        raise DbError("shop_id 无效")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, shop_id, goods_name, goods_link, problem, tb_item_id,
                       created_at, updated_at
                FROM goods_tb WHERE shop_id = %s
                ORDER BY updated_at DESC, id DESC
                """,
                (shop_pk,),
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "shop_id": r[1],
            "goods_name": r[2],
            "goods_link": r[3],
            "problem": r[4] or "",
            "tb_item_id": r[5],
            "created_at": _ts(r[6]),
            "updated_at": _ts(r[7]),
        }
        for r in rows
    ]


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def upsert_shop_uniq(
    shop_name: str,
    shop_link: str,
    shop_content: str,
) -> dict:
    """有区别才更新 shop_uniq_tb；内容相同则不动。

    匹配优先 shop_link；link 为空时用 shop_name。
    """
    name = (shop_name or "").strip()
    link = (shop_link or "").strip()
    content = shop_content if shop_content is not None else ""
    if not name and not link:
        raise DbError("shop_name 与 shop_link 不能同时为空")
    if not content.strip():
        raise DbError("shop_content 不能为空")
    new_hash = _content_hash(content)
    with connect() as conn:
        with conn.cursor() as cur:
            row = None
            if link:
                cur.execute(
                    """
                    SELECT id, shop_name, shop_link, shop_content, content_hash
                    FROM shop_uniq_tb WHERE shop_link = %s
                    """,
                    (link,),
                )
                row = cur.fetchone()
            if row is None and name:
                cur.execute(
                    """
                    SELECT id, shop_name, shop_link, shop_content, content_hash
                    FROM shop_uniq_tb WHERE shop_name = %s AND shop_link = ''
                    """,
                    (name,),
                )
                row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO shop_uniq_tb (shop_name, shop_link, shop_content, content_hash)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, shop_name, shop_link, content_hash, created_at, updated_at
                    """,
                    (name, link, content, new_hash),
                )
                r = cur.fetchone()
                return {
                    "ok": True,
                    "action": "inserted",
                    "id": r[0],
                    "shop_name": r[1],
                    "shop_link": r[2],
                    "content_hash": r[3],
                    "changed": True,
                }
            old_hash = (row[4] or "").strip()
            if old_hash == new_hash or (row[3] or "") == content:
                return {
                    "ok": True,
                    "action": "unchanged",
                    "id": row[0],
                    "shop_name": row[1],
                    "shop_link": row[2],
                    "content_hash": old_hash,
                    "changed": False,
                }
            cur.execute(
                """
                UPDATE shop_uniq_tb SET
                    shop_name = CASE WHEN %s <> '' THEN %s ELSE shop_name END,
                    shop_link = CASE WHEN %s <> '' THEN %s ELSE shop_link END,
                    shop_content = %s,
                    content_hash = %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING id, shop_name, shop_link, content_hash
                """,
                (name, name, link, link, content, new_hash, row[0]),
            )
            r = cur.fetchone()
            return {
                "ok": True,
                "action": "updated",
                "id": r[0],
                "shop_name": r[1],
                "shop_link": r[2],
                "content_hash": r[3],
                "changed": True,
            }


ADMIN_DB_TABLES = (
    "user_tb",
    "shop_tb",
    "goods_tb",
    "scan_shop_tb",
    "sms_code_tb",
    "shop_uniq_tb",
)
_ADMIN_MASK_COLS = frozenset({"password_hash", "cookie"})
_ADMIN_TRIM_COLS = frozenset({"source_md", "shop_content", "problem", "judge_prompt"})


def set_shop_source_md(shop_pk: int, source_md: str) -> dict:
    """覆盖写入店铺源文件全文。空串也写（重扫后以本次为准）。"""
    if not shop_pk:
        raise DbError("shop_id 无效")
    text = source_md if source_md is not None else ""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE shop_tb SET source_md = %s, updated_at = now()
                WHERE id = %s
                RETURNING {_SHOP_LIST_COLS}
                """,
                (text, int(shop_pk)),
            )
            row = cur.fetchone()
    if not row:
        raise DbError("店铺不存在，无法写入源文件")
    return _shop_row(row)


def get_shop_source_md(owner_user_id: int, tb_shop_id: str) -> dict:
    tb_shop_id = str(tb_shop_id or "").strip()
    if not owner_user_id or not tb_shop_id:
        raise DbError("user_id / tb_shop_id 无效")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT shop_name, tb_shop_id, coalesce(source_md, '')
                FROM shop_tb
                WHERE user_id = %s AND tb_shop_id = %s
                """,
                (int(owner_user_id), tb_shop_id),
            )
            row = cur.fetchone()
    if not row:
        raise DbError("店铺不存在")
    text = row[2] or ""
    return {
        "ok": True,
        "shop_name": row[0] or row[1],
        "tb_shop_id": row[1] or "",
        "source_md": text,
        "has_source_md": bool(text.strip()),
        "bytes": len(text.encode("utf-8")),
    }


def admin_db_tables() -> list[dict]:
    out: list[dict] = []
    with connect() as conn:
        with conn.cursor() as cur:
            for name in ADMIN_DB_TABLES:
                cur.execute(f"SELECT count(*) FROM {name}")
                out.append({"name": name, "count": int(cur.fetchone()[0])})
    return out


def admin_db_rows(table: str, limit: int = 50, offset: int = 0) -> dict:
    name = str(table or "").strip()
    if name not in ADMIN_DB_TABLES:
        raise DbError(f"不允许查看表: {table}")
    try:
        limit_n = int(limit)
        offset_n = int(offset)
    except (TypeError, ValueError) as e:
        raise DbError(f"limit/offset 无效: {e}") from e
    if limit_n < 1 or limit_n > 200:
        raise DbError("limit 必须是 1–200")
    if offset_n < 0:
        raise DbError("offset 不能为负")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {name}")
            total = int(cur.fetchone()[0])
            cur.execute(
                f"SELECT * FROM {name} ORDER BY id DESC LIMIT %s OFFSET %s",
                (limit_n, offset_n),
            )
            cols = [d[0] for d in cur.description]
            raw_rows = cur.fetchall()
    rows = []
    for raw in raw_rows:
        item = {}
        for col, val in zip(cols, raw):
            if col in _ADMIN_MASK_COLS:
                item[col] = "******" if val else ""
            elif col in _ADMIN_TRIM_COLS:
                s = "" if val is None else str(val)
                item[col] = s if len(s) <= 120 else (s[:120] + f"…({len(s)}字)")
            elif isinstance(val, datetime):
                item[col] = val.isoformat()
            else:
                try:
                    json.dumps(val, default=str)
                    item[col] = val if not hasattr(val, "as_tuple") else float(val)
                except TypeError:
                    item[col] = str(val)
        rows.append(item)
    return {
        "ok": True,
        "table": name,
        "columns": cols,
        "rows": rows,
        "total": total,
        "limit": limit_n,
        "offset": offset_n,
    }


def health_db() -> dict:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), count(*) FROM user_tb")
            dbname, user_n = cur.fetchone()
            cur.execute("SELECT count(*) FROM shop_tb")
            shop_n = cur.fetchone()[0]
            try:
                cur.execute("SELECT count(*) FROM goods_tb")
                goods_n = cur.fetchone()[0]
            except Exception:  # noqa: BLE001
                goods_n = -1
            try:
                cur.execute("SELECT count(*) FROM shop_uniq_tb")
                uniq_n = cur.fetchone()[0]
            except Exception:  # noqa: BLE001
                uniq_n = -1
    return {
        "ok": True,
        "database": dbname,
        "users": user_n,
        "shops": shop_n,
        "goods": goods_n,
        "shop_uniq": uniq_n,
    }
