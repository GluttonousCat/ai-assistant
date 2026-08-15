"""
Cookie 管理节点
- check_cookie_node:       校验当前 Cookie 有效性
- handle_cookie_input_node: 接收并保存用户提供的 Cookie
"""
from datetime import datetime

from agent.state import AgentState, CookieStatus


def check_cookie_node(state: AgentState) -> AgentState:
    """校验 Cookie 有效性"""
    state.log("检查Cookie状态")

    cookie = state.get_active_cookie()
    if not cookie:
        state.log("⚠️ 无可用Cookie")
        state.response = "请先提供Cookie。请发送：设置cookie [你的cookie]"
        return state

    try:
        import requests
        headers = {
            "Cookie": cookie,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36"
            ),
        }
        resp = requests.get(
            "https://api.zsxq.com/v2/users/self",
            headers=headers,
            timeout=10,
        )

        if resp.status_code == 200:
            data = resp.json()
            if data.get("succeeded"):
                state.log("✅ Cookie有效")
                if state.active_cookie:
                    state.cookies[state.active_cookie].is_valid = True
                    state.cookies[state.active_cookie].last_checked = datetime.now()
            else:
                state.log(f"❌ Cookie失效: {data.get('error')}")
                state.invalidate_cookie(state.active_cookie)
                state.response = "Cookie已失效，请重新提供"
        else:
            state.log(f"❌ HTTP错误: {resp.status_code}")
            state.invalidate_cookie(state.active_cookie)
            state.response = "Cookie验证失败，请检查"

    except Exception as e:
        state.log(f"⚠️ 验证异常: {e}")

    return state


def handle_cookie_input_node(state: AgentState) -> AgentState:
    """接收并保存用户输入的 Cookie"""
    text = state.user_input

    if "cookie" in text.lower() or "设置" in text:
        for line in text.split("\n"):
            if "=" in line and len(line) > 50:
                cookie = line.strip()
                cookie_key = f"cookie_{len(state.cookies)}"

                state.cookies[cookie_key] = CookieStatus(
                    cookie=cookie, last_checked=datetime.now()
                )
                state.active_cookie = cookie_key

                state.log(f"添加Cookie: {cookie_key}")
                state.response = "✅ Cookie已设置并验证"
                return state

    state.response = "无法识别Cookie格式，请直接粘贴"
    return state
