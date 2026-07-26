"""技术范式雷达环境变量配置向导。"""

from __future__ import annotations

import sys
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"

# (变量名, 显示名, 说明, 默认值, 是否必填)
SECTIONS: list[tuple[str, list[tuple[str, str, str, str, bool]]]] = [
    (
        "运行模式与模型",
        [
            ("PIPELINE_MODE", "运行模式", "paradigm=技术范式雷达；legacy=旧项目模式", "paradigm", True),
            ("LLM_PROVIDER", "LLM 提供商", "推荐 dashscope", "dashscope", True),
            ("LLM_MODEL", "全局模型", "Qwen 3.7 系列模型名", "qwen3.7-plus", True),
            ("LLM_API_KEY", "LLM API Key", "DashScope API Key", "", True),
            ("LLM_BASE_URL", "LLM Base URL", "OpenAI 兼容接口地址", "https://dashscope.aliyuncs.com/compatible-mode/v1", False),
            ("SUB_AGENT_MODEL", "论文抽取模型", "留空继承全局；建议 qwen3.7-plus", "qwen3.7-plus", False),
            ("MAIN_AGENT_MODEL", "人物分析模型", "留空继承全局；建议 qwen3.7-plus", "qwen3.7-plus", False),
        ],
    ),
    (
        "学术与证据信源",
        [
            ("OPENALEX_API_KEY", "OpenAlex Key", "免费 Key：https://openalex.org/settings/api", "", False),
            ("SEMANTIC_SCHOLAR_API_KEY", "Semantic Scholar Key", "只有获批 Key 并显式启用后才使用", "", False),
            ("OPENREVIEW_VENUES", "OpenReview Venues", "逗号分隔，需按会议年份核对", "ICLR.cc/2026/Conference,NeurIPS.cc/2026/Conference", False),
            ("RESEARCH_FEED_URLS", "官方研究 Feed", "已验证的官方 RSS/Atom URL，逗号分隔", "", False),
            ("GITHUB_TOKEN", "GitHub Token", "只用于搜索实现/复现；无权限 Token 即可", "", False),
        ],
    ),
    (
        "范式筛选",
        [
            ("PARADIGM_RUBRIC_PATH", "Rubric 文件", "留空使用 rubrics/paradigm_rubric.json", "", False),
            ("FRONTIER_LANDSCAPE_PATH", "前沿覆盖地图", "留空使用 taxonomy/frontier_landscape.json", "", False),
            ("PARADIGM_RECALL_OVERLAP_DAYS", "周更重叠发现天数", "默认 14；由数据库去重并修复单周漏抓", "14", False),
            ("PARADIGM_BOOTSTRAP_LOOKBACK_DAYS", "冷启动回看天数", "数据库为空时建立近期路线基线", "60", False),
            ("PARADIGM_SEED_ARXIV_IDS", "精确 arXiv 回补", "常规留空；审计漏项时用逗号分隔", "", False),
            ("PARADIGM_RESEARCHER_PROFILE_LIMIT", "每条路线人物上限", "默认覆盖前三位、末位与重点作者", "6", False),
            ("PARADIGM_DISCOVERY_SAFETY_LIMIT", "发现熔断", "0=不限制；只保护运行，不参与筛选", "0", False),
            ("PARADIGM_ANALYSIS_SAFETY_LIMIT", "抽取熔断", "0=分析全部待评估材料", "0", False),
            ("PARADIGM_DEEP_SAFETY_LIMIT", "深挖熔断", "0=深挖全部通过 Rubric 的路线", "0", False),
            ("PARADIGM_REPORT_SAFETY_LIMIT", "报告熔断", "0=输出全部通过最终 Rubric 的路线", "0", False),
            ("PARADIGM_REFRESH_SAFETY_LIMIT", "历史刷新熔断", "0=刷新全部观察中路线", "0", False),
            ("PARADIGM_ALLOW_UPDATES", "允许进展更新", "有实质新证据时再次报告；相同签名仍会跳过", "true", False),
        ],
    ),
    (
        "周任务",
        [
            ("SOURCING_LOOKBACK_DAYS", "回看天数", "周报填 7，月度回顾填 30", "7", False),
            ("SCHEDULE_DAY_OF_WEEK", "运行日", "周五为 fri", "fri", False),
            ("SCHEDULE_HOUR", "运行小时", "24 小时制", "9", False),
            ("SCHEDULE_MINUTE", "运行分钟", "0-59", "0", False),
            ("SCHEDULE_TIMEZONE", "时区", "IANA 时区名", "Asia/Shanghai", False),
        ],
    ),
    (
        "邮件推送",
        [
            ("EMAIL_PUSH_ENABLED", "启用邮件", "true/false", "false", False),
            ("SMTP_HOST", "SMTP 服务器", "例如 smtp.qq.com", "", False),
            ("SMTP_PORT", "SMTP 端口", "SSL 常用 465", "465", False),
            ("SMTP_USERNAME", "发件账号", "完整邮箱", "", False),
            ("SMTP_PASSWORD", "SMTP 授权码", "使用应用密码/授权码，不用登录密码", "", False),
            ("SMTP_FROM", "发件地址", "留空则沿用账号", "", False),
            ("SMTP_TO", "收件地址", "多个地址用英文逗号分隔", "", False),
            ("SMTP_USE_SSL", "使用 SSL", "465 端口通常为 true", "true", False),
            ("SMTP_USE_STARTTLS", "使用 STARTTLS", "587 端口通常为 true；不要与 SSL 同开", "false", False),
        ],
    ),
    (
        "日志",
        [("LOG_LEVEL", "日志级别", "DEBUG / INFO / WARNING", "INFO", False)],
    ),
]


def _load_current() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _mask(key: str, value: str) -> str:
    if any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
        if len(value) > 8:
            return value[:4] + "****" + value[-4:]
        return "****"
    return value


def _print_status() -> None:
    current = _load_current()
    print("\n" + "=" * 60)
    print("  AI 技术范式雷达 — 环境配置状态")
    print("=" * 60)
    if not ENV_PATH.exists():
        print(f"\n  ⚠ .env 不存在：{ENV_PATH}\n")
        return
    for section_name, items in SECTIONS:
        print(f"\n  ┌─ {section_name}")
        for key, label, _, default, required in items:
            value = current.get(key, "")
            if value:
                status = f"✓ {_mask(key, value)}"
            elif default:
                status = f"· 默认: {default}"
            elif required:
                status = "✗ 未配置（必填）"
            else:
                status = "· 未配置（可选）"
            print(f"  │  {status:42s} ← {label}")
        print("  └─")
    try:
        from agents.llm_utils import resolve_all
        sub, main = resolve_all()
        print("\n  运行时模型：")
        print(f"  - 论文抽取：{sub.label}")
        print(f"  - 人物分析：{main.label}")
    except Exception as exc:
        print(f"\n  ⚠ 模型解析失败：{exc}")
    print()


def run_setup() -> None:
    current = _load_current()
    values = dict(current)
    print("\n" + "=" * 60)
    print("  AI 技术范式雷达 — 环境配置向导")
    print("  直接回车保留当前值/使用默认值；输入 skip 跳过分组")
    print("=" * 60)
    for section_name, items in SECTIONS:
        print(f"\n── {section_name}")
        if input("  配置此分组？[Y/n/skip] ").strip().lower() in {"n", "s", "skip"}:
            continue
        for key, label, description, default, _ in items:
            current_value = current.get(key, "")
            shown = f"当前: {_mask(key, current_value)}" if current_value else f"默认: {default}" if default else "可选"
            entered = input(f"  {label}（{shown}）\n    {description}\n    → ").strip()
            if entered:
                values[key] = entered
            elif not current_value and default:
                values[key] = default
    _write_env(values)
    print(f"\n  ✓ 已保存：{ENV_PATH}")
    _print_status()


def _write_env(values: dict[str, str]) -> None:
    lines = [
        "# AI 技术范式雷达环境配置",
        "# 由 setup_env.py 生成；不要提交真实密钥",
        "",
    ]
    for section_name, items in SECTIONS:
        lines.append(f"# ── {section_name}")
        for key, *_ in items:
            value = values.get(key, "")
            lines.append(f"{key}={value}" if value else f"# {key}=")
        lines.append("")
    known = {key for _, items in SECTIONS for key, *_ in items}
    extras = {key: value for key, value in values.items() if key not in known and value}
    if extras:
        lines.append("# ── 兼容旧版/自定义变量")
        lines.extend(f"{key}={value}" for key, value in extras.items())
        lines.append("")
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        _print_status()
    else:
        run_setup()
