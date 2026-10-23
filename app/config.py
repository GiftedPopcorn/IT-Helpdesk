"""集中配置管理。

照搬 MindBridge 的 Settings(BaseSettings) 模式：
- 所有配置项从环境变量 / .env 文件读取，带类型校验和默认值
- get_settings() 用 lru_cache 保证全局单例，避免重复解析 .env
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- LLM 配置：provider 差异全部收敛为配置项，切换 = 改 .env 一行 ----
    llm_provider: str = "mock"  # deepseek | openai | mock
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.2  # 低温度 = 少发挥，IT 支持场景要严谨

    # ---- Agent 循环护栏 ----
    agent_max_steps_per_turn: int = 8  # 单轮内最多 LLM 调用次数（防死循环/控成本）
    agent_confidence_floor: float = 0.7  # 置信度地板：低于此值先追问而非硬答
    agent_max_context_messages: int = 12  # 构建提示时最多携带的最近消息条数

    # ---- 工具执行护栏 ----
    tool_timeout_seconds: int = 15  # 单个工具调用最大耗时，超时返回 timeout 错误

    # ---- 路径 ----
    data_dir: str = "data"
    trace_log_path: str = "logs/trace.jsonl"


@lru_cache
def get_settings() -> Settings:
    return Settings()
