"""HTTP GET handlers + WebSocket message handlers for the portal."""

import json
from typing import Any

from loguru import logger

from nanobot.portal import bridge
from nanobot.portal.auth import (
    create_token,
    hash_password,
    verify_password,
    verify_token,
)
from nanobot.portal.db import get_db

try:
    from websockets.http11 import Response as WsResponse
    from websockets.datastructures import Headers as WsHeaders
except ImportError:
    WsResponse = None  # type: ignore
    WsHeaders = None  # type: ignore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_resp(status: int, body: Any) -> "WsResponse":
    data = json.dumps(body, ensure_ascii=False).encode()
    headers = WsHeaders([
        ("Content-Type", "application/json; charset=utf-8"),
        ("Access-Control-Allow-Origin", "*"),
    ])
    phrase = {200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized",
              402: "Payment Required", 403: "Forbidden", 404: "Not Found",
              409: "Conflict"}.get(status, "Error")
    return WsResponse(status, phrase, headers, data)


# ---------------------------------------------------------------------------
# GET handler (called from process_request — only GET reaches here)
# ---------------------------------------------------------------------------

async def handle_get_request(
    path: str,
    token: str | None = None,
    serve_html_fn: Any = None,
    query_params: dict | None = None,
) -> "WsResponse | None":
    """Handle GET requests via process_request hook."""

    # Static page
    if path == "/" or path == "":
        if serve_html_fn:
            return serve_html_fn()
        return _json_resp(404, {"error": "no frontend"})

    # Public: no token needed
    # (none currently)

    # All API routes below need token
    user = verify_token(token) if token else None

    if path == "/api/auth/me":
        if not user:
            return _json_resp(401, {"error": "unauthorized"})
        return await _get_me(user)

    if path == "/api/marketplace":
        if not user:
            return _json_resp(401, {"error": "unauthorized"})
        return await _get_marketplace()

    if path == "/api/owner/listings":
        if not user:
            return _json_resp(401, {"error": "unauthorized"})
        return await _get_owner_listings(user)

    if path == "/api/xiandou/balance":
        if not user:
            return _json_resp(401, {"error": "unauthorized"})
        return await _get_balance(user)

    if path == "/api/xiandou/history":
        if not user:
            return _json_resp(401, {"error": "unauthorized"})
        return await _get_tx_history(user)

    if path == "/api/employees":
        return _json_resp(200, bridge.get_employees())

    if path == "/api/skill-content":
        if not user:
            return _json_resp(401, {"error": "unauthorized"})
        skill_name = (query_params or {}).get("name", "")
        if not skill_name:
            return _json_resp(400, {"error": "缺少 name 参数"})
        content = bridge.read_skill_content(skill_name)
        return _json_resp(200, {"name": skill_name, "content": content or "", "exists": content is not None})

    # Admin endpoints
    if path == "/api/admin/users":
        if not user or user.get("role") != "admin":
            return _json_resp(403, {"error": "仅管理员可访问"})
        return await _get_admin_users()

    if path == "/api/admin/stats":
        if not user or user.get("role") != "admin":
            return _json_resp(403, {"error": "仅管理员可访问"})
        return await _get_admin_stats()

    return _json_resp(404, {"error": "not found"})


# ---------------------------------------------------------------------------
# WebSocket message handler (all mutations go through here)
# ---------------------------------------------------------------------------

async def handle_ws_message(msg: dict) -> dict:
    """Handle a WebSocket JSON message. Returns response dict."""
    msg_type = msg.get("type", "")
    data = msg.get("data", {})
    token = msg.get("token")

    if msg_type == "register":
        return await _ws_register(data)
    if msg_type == "login":
        return await _ws_login(data)

    # All other actions require auth
    user = verify_token(token) if token else None
    if not user:
        return {"type": f"{msg_type}_result", "ok": False, "error": "unauthorized"}

    if msg_type == "create_listing":
        return await _ws_create_listing(user, data)
    if msg_type == "delete_listing":
        return await _ws_delete_listing(user, data)
    if msg_type == "save_skill":
        return await _ws_save_skill(user, data)
    if msg_type == "use_agent":
        return await _ws_use_agent(user, data)
    if msg_type == "topup":
        return await _ws_topup(user, data)
    if msg_type == "admin_update_user":
        return await _ws_admin_update_user(user, data)

    return {"type": "error", "error": f"unknown message type: {msg_type}"}


# ---------------------------------------------------------------------------
# GET implementations
# ---------------------------------------------------------------------------

async def _get_me(user: dict) -> "WsResponse":
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, username, display_name, role, created_at FROM users WHERE id=?",
            (user["sub"],),
        )
        row = await cursor.fetchone()
        if not row:
            return _json_resp(404, {"error": "用户不存在"})
        return _json_resp(200, dict(row))
    finally:
        await db.close()


async def _get_marketplace() -> "WsResponse":
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT l.*, u.username AS owner_name FROM agent_listings l "
            "JOIN users u ON l.owner_id = u.id WHERE l.listed = 1 ORDER BY l.id DESC"
        )
        rows = await cursor.fetchall()
        listings = []
        for r in rows:
            d = dict(r)
            port = bridge.get_agent_port(d["employee_id"])
            d["port"] = port or 0
            d["online"] = port is not None
            listings.append(d)
        return _json_resp(200, listings)
    finally:
        await db.close()


async def _get_owner_listings(user: dict) -> "WsResponse":
    if user.get("role") not in ("owner", "admin"):
        return _json_resp(403, {"error": "仅 owner 可管理上架"})
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM agent_listings WHERE owner_id=? ORDER BY id DESC",
            (user["sub"],),
        )
        rows = await cursor.fetchall()
        listings = []
        for r in rows:
            d = dict(r)
            port = bridge.get_agent_port(d["employee_id"])
            d["port"] = port or 0
            d["online"] = port is not None
            listings.append(d)
        return _json_resp(200, listings)
    finally:
        await db.close()


async def _get_balance(user: dict) -> "WsResponse":
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT balance FROM xiandou_accounts WHERE user_id=?", (user["sub"],)
        )
        row = await cursor.fetchone()
        return _json_resp(200, {"balance": row["balance"] if row else 0})
    finally:
        await db.close()


async def _get_tx_history(user: dict) -> "WsResponse":
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM transactions WHERE from_user_id=? OR to_user_id=? "
            "ORDER BY id DESC LIMIT 100",
            (user["sub"], user["sub"]),
        )
        rows = await cursor.fetchall()
        return _json_resp(200, [dict(r) for r in rows])
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# WebSocket mutation implementations
# ---------------------------------------------------------------------------

async def _ws_register(data: dict) -> dict:
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()
    display_name = str(data.get("display_name", "")).strip() or username
    role = str(data.get("role", "user")).strip()

    if not username or len(username) < 3:
        return {"type": "register_result", "ok": False, "error": "用户名至少3个字符"}
    if not password or len(password) < 6:
        return {"type": "register_result", "ok": False, "error": "密码至少6个字符"}
    if role not in ("owner", "user"):
        return {"type": "register_result", "ok": False, "error": "角色必须是 owner 或 user"}

    db = await get_db()
    try:
        existing = await db.execute("SELECT id FROM users WHERE username=?", (username,))
        if await existing.fetchone():
            return {"type": "register_result", "ok": False, "error": "用户名已存在"}

        pw_hash = hash_password(password)
        cursor = await db.execute(
            "INSERT INTO users (username, password_hash, display_name, role) VALUES (?,?,?,?)",
            (username, pw_hash, display_name, role),
        )
        user_id = cursor.lastrowid
        await db.execute(
            "INSERT INTO xiandou_accounts (user_id, balance) VALUES (?, 0)", (user_id,)
        )
        await db.commit()

        token = create_token(user_id, username, role)
        logger.info(f"Portal: new user registered — {username} ({role})")
        return {
            "type": "register_result", "ok": True,
            "token": token,
            "user": {"id": user_id, "username": username, "display_name": display_name, "role": role},
        }
    except Exception as e:
        logger.error(f"Portal register error: {e}")
        return {"type": "register_result", "ok": False, "error": str(e)}
    finally:
        await db.close()


async def _ws_login(data: dict) -> dict:
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()

    if not username or not password:
        return {"type": "login_result", "ok": False, "error": "用户名和密码不能为空"}

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, username, password_hash, display_name, role, created_at FROM users WHERE username=?",
            (username,),
        )
        row = await cursor.fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            return {"type": "login_result", "ok": False, "error": "用户名或密码错误"}

        token = create_token(row["id"], row["username"], row["role"])
        return {
            "type": "login_result", "ok": True,
            "token": token,
            "user": {
                "id": row["id"], "username": row["username"],
                "display_name": row["display_name"], "role": row["role"],
                "created_at": row["created_at"],
            },
        }
    finally:
        await db.close()


async def _ws_create_listing(user: dict, data: dict) -> dict:
    if user.get("role") not in ("owner", "admin"):
        return {"type": "create_listing_result", "ok": False, "error": "仅 owner 可上架"}

    employee_id = str(data.get("employee_id", "")).strip()
    employee_name = str(data.get("employee_name", "")).strip()
    skill = str(data.get("skill", "")).strip()
    price = int(data.get("price", 0))
    description = str(data.get("description", "")).strip()

    if not employee_id or not employee_name:
        return {"type": "create_listing_result", "ok": False, "error": "employee_id 和 employee_name 为必填"}

    db = await get_db()
    try:
        existing = await db.execute(
            "SELECT id FROM agent_listings WHERE owner_id=? AND employee_id=?",
            (user["sub"], employee_id),
        )
        row = await existing.fetchone()
        if row:
            await db.execute(
                "UPDATE agent_listings SET employee_name=?, skill=?, price=?, description=?, listed=1 WHERE id=?",
                (employee_name, skill, price, description, row["id"]),
            )
        else:
            await db.execute(
                "INSERT INTO agent_listings (owner_id, employee_id, employee_name, skill, price, description) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user["sub"], employee_id, employee_name, skill, price, description),
            )
        await db.commit()
        logger.info(f"Portal: listing upserted — {employee_name} by user#{user['sub']}")

        # Sync to config.json → triggers ConfigWatcher → auto-start employee
        bridge.add_employee_to_config(employee_id, employee_name, skill)

        return {"type": "create_listing_result", "ok": True}
    finally:
        await db.close()


async def _ws_delete_listing(user: dict, data: dict) -> dict:
    if user.get("role") not in ("owner", "admin"):
        return {"type": "delete_listing_result", "ok": False, "error": "仅 owner 可下架"}

    listing_id = int(data.get("id", 0))
    if listing_id <= 0:
        return {"type": "delete_listing_result", "ok": False, "error": "无效的 listing id"}

    db = await get_db()
    try:
        # Fetch employee_id before updating
        cursor = await db.execute(
            "SELECT employee_id FROM agent_listings WHERE id=? AND owner_id=?",
            (listing_id, user["sub"]),
        )
        row = await cursor.fetchone()

        await db.execute(
            "UPDATE agent_listings SET listed=0 WHERE id=? AND owner_id=?",
            (listing_id, user["sub"]),
        )
        await db.commit()

        # Sync to config.json → triggers ConfigWatcher → auto-stop employee
        if row:
            bridge.remove_employee_from_config(row["employee_id"])

        return {"type": "delete_listing_result", "ok": True}
    finally:
        await db.close()


async def _ws_save_skill(user: dict, data: dict) -> dict:
    if user.get("role") not in ("owner", "admin"):
        return {"type": "save_skill_result", "ok": False, "error": "仅 owner 可编辑技能"}

    skill_name = str(data.get("skill_name", "")).strip()
    content = str(data.get("content", "")).strip()

    if not skill_name:
        return {"type": "save_skill_result", "ok": False, "error": "技能名称不能为空"}
    if not content:
        return {"type": "save_skill_result", "ok": False, "error": "技能内容不能为空"}

    ok = bridge.save_skill_content(skill_name, content)
    if not ok:
        return {"type": "save_skill_result", "ok": False, "error": "保存失败，请检查 workspace 配置"}
    return {"type": "save_skill_result", "ok": True}


async def _ws_use_agent(user: dict, data: dict) -> dict:
    listing_id = int(data.get("listing_id", 0))
    if listing_id <= 0:
        return {"type": "use_agent_result", "ok": False, "error": "无效的 listing id"}

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM agent_listings WHERE id=? AND listed=1", (listing_id,)
        )
        listing = await cursor.fetchone()
        if not listing:
            return {"type": "use_agent_result", "ok": False, "error": "数字员工不存在或已下架"}

        price = listing["price"]

        if price > 0:
            cursor = await db.execute(
                "SELECT balance FROM xiandou_accounts WHERE user_id=?", (user["sub"],)
            )
            acct = await cursor.fetchone()
            balance = acct["balance"] if acct else 0
            if balance < price:
                return {"type": "use_agent_result", "ok": False, "error": f"仙豆不足，需要 {price}，当前 {balance}"}

            await db.execute(
                "UPDATE xiandou_accounts SET balance = balance - ? WHERE user_id=?",
                (price, user["sub"]),
            )
            await db.execute(
                "UPDATE xiandou_accounts SET balance = balance + ? WHERE user_id=?",
                (price, listing["owner_id"]),
            )
            await db.execute(
                "INSERT INTO transactions (from_user_id, to_user_id, amount, tx_type, listing_id, memo) "
                "VALUES (?, ?, ?, 'payment', ?, ?)",
                (user["sub"], listing["owner_id"], price, listing_id,
                 f"使用数字员工 {listing['employee_name']}"),
            )
            await db.commit()

        port = bridge.get_agent_port(listing["employee_id"])
        return {
            "type": "use_agent_result", "ok": True,
            "employee_id": listing["employee_id"],
            "employee_name": listing["employee_name"],
            "port": port or 0,
            "charged": price,
        }
    finally:
        await db.close()


async def _ws_topup(user: dict, data: dict) -> dict:
    if user.get("role") != "admin":
        return {"type": "topup_result", "ok": False, "error": "仅管理员可充值"}

    target_user_id = int(data.get("user_id", 0))
    amount = int(data.get("amount", 0))
    if target_user_id <= 0 or amount <= 0:
        return {"type": "topup_result", "ok": False, "error": "user_id 和 amount 必须为正整数"}

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT user_id FROM xiandou_accounts WHERE user_id=?", (target_user_id,)
        )
        if not await cursor.fetchone():
            return {"type": "topup_result", "ok": False, "error": "目标用户不存在"}

        await db.execute(
            "UPDATE xiandou_accounts SET balance = balance + ? WHERE user_id=?",
            (amount, target_user_id),
        )
        await db.execute(
            "INSERT INTO transactions (from_user_id, to_user_id, amount, tx_type, memo) "
            "VALUES (NULL, ?, ?, 'topup', ?)",
            (target_user_id, amount, f"管理员充值 by {user['usr']}"),
        )
        await db.commit()
        logger.info(f"Portal: topup {amount} xiandou to user#{target_user_id}")
        return {"type": "topup_result", "ok": True, "amount": amount}
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Admin implementations
# ---------------------------------------------------------------------------

async def _get_admin_users() -> "WsResponse":
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT u.id, u.username, u.display_name, u.role, u.created_at, "
            "COALESCE(x.balance, 0) AS balance, "
            "(SELECT COUNT(*) FROM transactions WHERE from_user_id = u.id AND tx_type = 'payment') AS usage_count "
            "FROM users u LEFT JOIN xiandou_accounts x ON u.id = x.user_id "
            "ORDER BY u.id"
        )
        rows = await cursor.fetchall()
        return _json_resp(200, [dict(r) for r in rows])
    finally:
        await db.close()


async def _get_admin_stats() -> "WsResponse":
    db = await get_db()
    try:
        # Total users by role
        cursor = await db.execute(
            "SELECT role, COUNT(*) AS count FROM users GROUP BY role"
        )
        role_counts = {r["role"]: r["count"] for r in await cursor.fetchall()}

        # Total xiandou in circulation
        cursor = await db.execute("SELECT COALESCE(SUM(balance), 0) AS total FROM xiandou_accounts")
        total_xiandou = (await cursor.fetchone())["total"]

        # Top agents by usage (payment transactions)
        cursor = await db.execute(
            "SELECT l.employee_name, l.skill, l.employee_id, COUNT(t.id) AS usage_count, "
            "COALESCE(SUM(t.amount), 0) AS total_earned "
            "FROM agent_listings l "
            "LEFT JOIN transactions t ON t.listing_id = l.id AND t.tx_type = 'payment' "
            "WHERE l.listed = 1 "
            "GROUP BY l.id ORDER BY usage_count DESC LIMIT 10"
        )
        top_agents = [dict(r) for r in await cursor.fetchall()]

        # Top skills by usage
        cursor = await db.execute(
            "SELECT l.skill, COUNT(t.id) AS usage_count, "
            "COALESCE(SUM(t.amount), 0) AS total_earned "
            "FROM agent_listings l "
            "LEFT JOIN transactions t ON t.listing_id = l.id AND t.tx_type = 'payment' "
            "WHERE l.listed = 1 "
            "GROUP BY l.skill ORDER BY usage_count DESC LIMIT 10"
        )
        top_skills = [dict(r) for r in await cursor.fetchall()]

        # Recent transactions
        cursor = await db.execute(
            "SELECT t.*, "
            "u1.username AS from_username, u2.username AS to_username "
            "FROM transactions t "
            "LEFT JOIN users u1 ON t.from_user_id = u1.id "
            "LEFT JOIN users u2 ON t.to_user_id = u2.id "
            "ORDER BY t.id DESC LIMIT 20"
        )
        recent_txs = [dict(r) for r in await cursor.fetchall()]

        # Total listings
        cursor = await db.execute("SELECT COUNT(*) AS count FROM agent_listings WHERE listed = 1")
        total_listings = (await cursor.fetchone())["count"]

        return _json_resp(200, {
            "role_counts": role_counts,
            "total_xiandou": total_xiandou,
            "total_listings": total_listings,
            "top_agents": top_agents,
            "top_skills": top_skills,
            "recent_txs": recent_txs,
        })
    finally:
        await db.close()


async def _ws_admin_update_user(user: dict, data: dict) -> dict:
    if user.get("role") != "admin":
        return {"type": "admin_update_user_result", "ok": False, "error": "仅管理员可操作"}

    target_id = int(data.get("user_id", 0))
    if target_id <= 0:
        return {"type": "admin_update_user_result", "ok": False, "error": "无效的 user_id"}

    new_role = data.get("role")
    if new_role and new_role not in ("owner", "user", "admin"):
        return {"type": "admin_update_user_result", "ok": False, "error": "无效的角色"}

    db = await get_db()
    try:
        if new_role:
            await db.execute("UPDATE users SET role=? WHERE id=?", (new_role, target_id))
            await db.commit()
            logger.info(f"Portal: admin changed user#{target_id} role to {new_role}")
        return {"type": "admin_update_user_result", "ok": True}
    finally:
        await db.close()
