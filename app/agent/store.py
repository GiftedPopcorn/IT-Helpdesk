"""会话存储：内存 dict + 线程锁。

接口化设计：换 Redis / DB 只需新增一个实现类，AgentLoop 不感知存储介质。
生产化路线图中包含持久化（会话恢复、升级工单落库）。
"""
import threading
from typing import Dict

from app.agent.state import SessionState, new_session_id


class SessionStore:
    def __init__(self):
        self._sessions: Dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str = "") -> SessionState:
        with self._lock:
            sid = session_id or new_session_id()
            if sid not in self._sessions:
                self._sessions[sid] = SessionState(session_id=sid)
            return self._sessions[sid]

    def reset(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None
