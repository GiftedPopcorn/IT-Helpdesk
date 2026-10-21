"""P0 冒烟脚本：验证 LLM 适配层打通。

用法：
    python smoke.py            # 按 .env 的 LLM_PROVIDER 跑（默认 mock，无需 API Key）
    python smoke.py --provider deepseek    # 用 DeepSeek 真实调用（需 .env 配好 Key）

通过标准：mock 模式返回脚本化 JSON；deepseek 模式返回一句真实模型回复。
"""
import argparse
import json
import sys

from app.config import get_settings
from app.llm import MockLLM, OpenAICompatLLM, build_llm


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default=None, help="覆盖 .env 的 LLM_PROVIDER")
    args = parser.parse_args()

    settings = get_settings()
    if args.provider:
        settings.llm_provider = args.provider

    if settings.llm_provider == "mock":
        llm = MockLLM()
    elif settings.llm_provider in ("deepseek", "openai"):
        llm = OpenAICompatLLM(settings)
    else:
        llm = build_llm(settings)

    print(f"[smoke] provider={settings.llm_provider} model={settings.llm_model}")
    resp = llm.complete(
        [{"role": "user", "content": "你好，请用一句话介绍你自己"}],
        temperature=0.2,
    )
    print(f"[smoke] finish_reason={resp.finish_reason} usage={resp.usage}")
    if resp.content:
        try:
            # mock 返回的是 JSON 契约，pretty print 一下
            print("[smoke] content (json):")
            print(json.dumps(json.loads(resp.content), ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            print(f"[smoke] content: {resp.content}")
    if resp.tool_calls:
        print(f"[smoke] tool_calls: {resp.tool_calls}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
