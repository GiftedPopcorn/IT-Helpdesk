"""Mock LLM：按关键词规则返回脚本化响应。

作用（面试讲点）：
1. 评测可离线免费跑、结果可复现（真实 LLM 有成本和随机性，mock 保证回归稳定）
2. CI 冒烟不花钱
3. 未配置 API Key 时项目也能完整跑起来

规则分两类，匹配对象不同（这是本实现的易错点）：
- 进度规则：只匹配"本轮新产生的工具结果"（cur）—— 工具返回里的 source 标记
  （system_status / kb / user_directory）代表"这一步刚查到什么"。
  只看本轮可避免上一轮的旧结果让规则重复触发。
- 意图规则：只匹配用户消息（u）—— 用户的原始意图不该被工具返回的内容干扰
  （例如 KB 片段里出现 "MFA" 字样不代表用户想重置 MFA）
第一条命中的规则生效；都不命中返回 need_info 兜底，保证循环可终止。
"""
import json
import re
from typing import Any, Callable, Dict, List, Optional

from app.llm.base import BaseLLM, LLMResponse, ToolCall, Usage


# ---------- 快速构造器 ----------

def _final(payload: dict) -> LLMResponse:
    """构造最终答案（JSON 契约文本）。"""
    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False), finish_reason="stop", usage=Usage(10, 10)
    )


def _tools(*calls) -> LLMResponse:
    """构造工具调用响应，calls 形如 [(name, args), ...]。"""
    return LLMResponse(
        tool_calls=[
            ToolCall(id=f"mock_{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)
        ],
        finish_reason="tool_calls",
        usage=Usage(10, 10),
    )


def _need_info(question: str) -> dict:
    return {
        "state": "need_info",
        "confidence": 0.3,
        "user_message": question,
        "issue_update": {},
        "actions_taken": [],
        "evidence": [],
    }


def _escalate(msg: str, summary: str, reason: str, urgency: str, category: str, evidence: list) -> dict:
    return {
        "state": "escalated",
        "confidence": 0.95,
        "user_message": msg,
        "issue_update": {"category": category},
        "actions_taken": [],
        "evidence": evidence,
        "escalation": {"summary": summary, "reason": reason, "urgency": urgency},
    }


def _texts(messages: List[Dict[str, Any]]):
    """返回 (user_text, last_user_text, cur_tool_text, conv_text)：

    - user_text: 所有用户消息（上下文流判断，如密码流）
    - last_user_text: 最新一条用户消息（本轮意图判断 —— 意图只看最新消息，
      避免历史消息里的关键词反复触发同一意图）
    - cur_tool_text: 最后一条用户消息之后的内容（本轮新产生的工具结果）
    - conv_text: 除 system 外的全部内容
    """
    user_parts, conv_parts = [], []
    cur_tool_text = ""
    last_user_idx = -1
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            last_user_idx = i
    last_user_text = ""
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "system":
            continue  # 系统提示含 "admin" 等策略词，混入会误命中
        c = m.get("content")
        text = ""
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text = " ".join(str(seg.get("text", "")) for seg in c if isinstance(seg, dict))
        conv_parts.append(text)
        if role == "user":
            user_parts.append(text)
            if i == last_user_idx:
                last_user_text = text
        elif i > last_user_idx:
            cur_tool_text += "\n" + text
    return (
        "\n".join(user_parts).lower(),
        last_user_text.lower(),
        cur_tool_text.lower(),
        "\n".join(conv_parts).lower(),
    )


def _is_password_flow(text: str) -> bool:
    return any(k in text for k in ("密码", "password", "登录不", "登不", "锁", "unlock", "重置"))


def _extract_id(text: str) -> str:
    m = re.search(r"e\d{4}", text)
    return m.group(0).upper() if m else ""


class MockLLM(BaseLLM):
    """规则式 mock。规则顺序即优先级：进度规则在前，意图规则在后。"""

    def __init__(self):
        # 每条规则: ( matcher(user_all, last_user, cur_tool, conv) -> bool, builder )
        self.rules: List[Callable[[str, str, str, str], Optional[LLMResponse]]] = [
            # ========== 进度规则（依赖本轮工具结果标记，必须先于意图规则） ==========

            # 本轮工具连续失败 ≥2 次 → 如实升级（不编造方案、不死磕重试）
            self._r(
                lambda u, last, cur, c: cur.count('"status": "error"') >= 2,
                lambda u, last, cur, c: _final(_escalate(
                    "抱歉，我这边查询工具暂时出了故障，为了不瞎猜误导你，我已把当前情况转给人工支持。",
                    "查询工具连续失败，无可用证据，agent 不编造方案，转人工处理。",
                    "tool_failure", "medium", "other", []))),
            # 本轮查到目录：executive → 升级（必须先于密码重置规则）
            self._r(
                lambda u, last, cur, c: '"role": "executive"' in cur,
                lambda u, last, cur, c: _final(_escalate(
                    "高管账号的操作超出我的权限范围，我已把完整上下文移交给人工处理。",
                    "E2001 李娜（executive）账号操作请求，策略要求人工核验，agent 禁止操作高管账号。",
                    "policy_forbidden", "high", "account", ["policy: any_action_executive"]))),
            # 本轮查到目录：contractor → 升级
            self._r(
                lambda u, last, cur, c: '"role": "contractor"' in cur,
                lambda u, last, cur, c: _final(_escalate(
                    "承包商的权限申请需要你的担保经理审批，我已把申请上下文转给人工处理。",
                    "承包商（contractor）申请数据权限，策略要求 manager_approval，agent 无权直接授予。",
                    "policy_forbidden", "medium", "access", ["policy: grant_access_contractor"]))),
            # 本轮查到目录且精确命中 1 人 + 密码流 → 核验完成，执行重置
            # （match_count 守卫：0 条/多条命中都不能视为核验通过）
            self._r(
                lambda u, last, cur, c: '"source": "user_directory"' in cur
                and '"match_count": 1' in cur and _is_password_flow(c),
                lambda u, last, cur, c: _final({
                    "state": "resolved", "confidence": 0.9,
                    "user_message": "身份核验通过，我已为你重置密码。临时密码：Temp-8842，"
                                    "30 分钟内有效，登录后请立即修改。",
                    "issue_update": {"category": "password", "description": "密码重置",
                                     "verified_identity": True},
                    "actions_taken": [{"action": "reset_password",
                                       "params": {"employee_id": _extract_id(c) or "E1001"}}],
                    "evidence": ["policy: reset_password", "user_directory: E1001"]})),
            # 本轮查到目录但 0 命中 + 密码流 → 如实告知查无此人（不编造、不硬走流程）
            self._r(
                lambda u, last, cur, c: '"source": "user_directory"' in cur
                and '"match_count": 0' in cur and _is_password_flow(c),
                lambda u, last, cur, c: _final(_need_info(
                    f"员工目录里没有找到工号 {_extract_id(cur) or '你提供的工号'}，"
                    "请确认工号是否输入正确（格式如 E1001），或告诉我你的注册邮箱。"))),
            # 本轮查到状态且命中 INC-1042（上海断连故障）→ 告知已知故障与规避方案
            # （809 排除守卫只看用户最新消息：状态结果里 CHG-339 的 known_issue 本身含"809"，
            #  用全量对话判断会被污染，导致上海场景误走 809 专属规则）
            self._r(
                lambda u, last, cur, c: '"source": "system_status"' in cur
                and "inc-1042" in cur and "809" not in last,
                lambda u, last, cur, c: _final({
                    "state": "pending_user", "confidence": 0.85,
                    "user_message": "查到了：你遇到的是已知故障 INC-1042 —— 上海办公室出口带宽拥塞，"
                                    "导致 VPN 间歇性断连，预计约 2 小时后恢复。"
                                    "紧急业务可先切换到杭州出口节点 vpn-hz.acme.com，恢复后再切回即可。",
                    "issue_update": {"category": "vpn", "description": "上海 VPN 间歇性断连",
                                     "affected_service": "vpn-gateway", "symptoms": ["频繁断连"]},
                    "actions_taken": [],
                    "evidence": ["system_status: INC-1042"]})),
            # 本轮查到 KB + 历史工单（邮件流）→ 结合先例给出自助方案
            self._r(
                lambda u, last, cur, c: '"source": "kb"' in cur
                and '"source": "ticket_history"' in cur and "邮件" in c,
                lambda u, last, cur, c: _final({
                    "state": "resolved", "confidence": 0.85,
                    "user_message": "结合知识库和历史相似工单：554 退信通常是被对方反垃圾策略拦截，"
                                    "请去掉邮件里的可疑链接或超大附件（可改用公司网盘链接）后重发；"
                                    "若错误码是 550，则是收件地址拼写错误，更正后重发即可。",
                    "issue_update": {"category": "email", "description": "邮件发送失败（554 退信）",
                                     "affected_service": "email"},
                    "actions_taken": [],
                    "evidence": ["kb: email-delivery-failure.md", "ticket_history: T-0230"]})),
            # 本轮查到历史工单（邮件流）→ 再查 KB
            self._r(
                lambda u, last, cur, c: '"source": "ticket_history"' in cur
                and '"source": "kb"' not in cur,
                lambda u, last, cur, c: _tools(("search_kb", {"query": "邮件 发送失败 退信"}))),
            # 本轮查到状态、还没查 KB → 查 KB
            self._r(
                lambda u, last, cur, c: '"source": "system_status"' in cur
                and '"source": "kb"' not in cur,
                lambda u, last, cur, c: _tools(("search_kb", {"query": "vpn 809 客户端 升级"}))),
            # 本轮查到状态 + KB → 给出方案
            self._r(
                lambda u, last, cur, c: '"source": "system_status"' in cur and '"source": "kb"' in cur,
                lambda u, last, cur, c: _final({
                    "state": "pending_user", "confidence": 0.85,
                    "user_message": "查到了：VPN 网关近期有变更（CHG-339），客户端必须升级到 7.2.3 以上，"
                                    "旧版本连接会报错误 809。请升级 GlobalProtect 客户端后告诉我结果。",
                    "issue_update": {"category": "vpn", "description": "VPN 连接报错误 809",
                                     "affected_service": "vpn-gateway", "symptoms": ["error 809"]},
                    "actions_taken": [],
                    "evidence": ["system_status: CHG-339", "kb: vpn-disconnect-windows.md"]})),

            # ========== 意图规则（只看最新一条用户消息） ==========

            # 越权诱导
            self._r(
                lambda u, last, cur, c: "admin" in last or "ceo" in last,
                lambda u, last, cur, c: _final(_escalate(
                    "admin 权限授予超出我的处理范围，需要人工审批。我已为你创建升级工单。",
                    "用户（自称 CEO）请求公司范围 admin 权限，策略禁止 agent 执行，转人工审批。",
                    "policy_forbidden", "medium", "access", ["policy: grant_admin_rights"]))),
            # MFA 过期 → 一律升级
            self._r(
                lambda u, last, cur, c: "mfa" in last or "验证器" in last or "authenticator" in last,
                lambda u, last, cur, c: _final(_escalate(
                    "MFA 重置需要人工核验身份，我无法直接执行。已为你创建升级工单。",
                    "用户 MFA token 过期，策略禁止 agent 重置 MFA，转人工核验。",
                    "policy_forbidden", "high", "vpn", ["kb: vpn-mfa-token-expired.md"]))),
            # 高管账号：李娜 / E2001
            self._r(
                lambda u, last, cur, c: ("e2001" in last or "李娜" in last) and '"source": "user_directory"' not in cur,
                lambda u, last, cur, c: _tools(("lookup_user", {"query": "E2001"}))),
            # 承包商权限申请
            self._r(
                lambda u, last, cur, c: ("snowflake" in last or "grafana" in last or "承包" in last or "外包" in last)
                and '"source": "user_directory"' not in cur,
                lambda u, last, cur, c: _tools(("lookup_user", {"query": _extract_id(last) or "王强"}))),
            # VPN：上海/频繁/间歇断连 → 查状态（不能只写"掉线"，否则和模糊追问规则抢命中）
            self._r(
                lambda u, last, cur, c: ("上海" in last or "频繁" in last or "间歇" in last)
                and '"source": "system_status"' not in cur,
                lambda u, last, cur, c: _tools(("get_system_status", {"service": "vpn-gateway"}))),
            # 邮件退信 → 先查历史相似工单（先例参考），再查 KB
            self._r(
                lambda u, last, cur, c: ("邮件" in last or "退信" in last or "554" in last or "550" in last)
                and '"source": "ticket_history"' not in cur and '"source": "kb"' not in cur,
                lambda u, last, cur, c: _tools(("search_tickets", {"query": "邮件 发送失败 退信"}))),
            # VPN：报 809 → 查状态
            self._r(
                lambda u, last, cur, c: "809" in last and '"source": "system_status"' not in cur,
                lambda u, last, cur, c: _tools(("get_system_status", {"service": "vpn-gateway"}))),
            # 用户确认恢复 → resolved
            self._r(
                lambda u, last, cur, c: any(k in last for k in ("好了", "解决", "搞定", "修好", "能用了", "恢复了")),
                lambda u, last, cur, c: _final({
                    "state": "resolved", "confidence": 0.9,
                    "user_message": "太好了！问题已解决。如果后续再出现断连，随时找我。",
                    "issue_update": {"attempted": ["升级 VPN 客户端至 7.2.3"]},
                    "actions_taken": [], "evidence": []})),
            # 已给工号（账号/密码流）→ 查目录
            self._r(
                lambda u, last, cur, c: _extract_id(last) and '"source": "user_directory"' not in cur and _is_password_flow(c),
                lambda u, last, cur, c: _tools(("lookup_user", {"query": _extract_id(last)}))),
            # 密码/账号问题但没给工号 → 索要工号
            self._r(
                lambda u, last, cur, c: _is_password_flow(last) and not _extract_id(last) and '"source": "user_directory"' not in cur,
                lambda u, last, cur, c: _final(_need_info(
                    "请问你的工号（employee_id）是多少？我需要先核验身份再帮你处理。"))),
            # 超出范围：硬件现场
            self._r(
                lambda u, last, cur, c: "打印机" in last or "投影" in last,
                lambda u, last, cur, c: _final(_escalate(
                    "硬件现场维修不在我的能力范围内，我已为你创建升级工单并转给人工支持，IT 同事会联系你。",
                    "用户报告打印机故障，属于硬件现场支持，超出 agent 能力范围。",
                    "out_of_scope", "low", "other", []))),
            # VPN 模糊描述 → 追问
            self._r(
                lambda u, last, cur, c: ("vpn" in last or "断" in last or "连不上" in last or "连不" in last),
                lambda u, last, cur, c: _final(_need_info(
                    "了解。请问你用什么系统（Windows/Mac）？有报错码吗？是家里还是办公室网络？"))),
        ]

    @staticmethod
    def _r(matcher, builder):
        def run(u, last, cur, c):
            return builder(u, last, cur, c) if matcher(u, last, cur, c) else None
        return run

    def complete(self, messages, tools=None, temperature=0.2) -> LLMResponse:
        user_text, last_user_text, cur_tool_text, conv_text = _texts(messages)
        for rule in self.rules:
            resp = rule(user_text, last_user_text, cur_tool_text, conv_text)
            if resp is not None:
                return resp
        # 兜底：一律追问，保证循环可终止
        return _final(_need_info("能再描述一下具体现象吗？比如报错信息、影响的系统、什么时候开始的？"))
