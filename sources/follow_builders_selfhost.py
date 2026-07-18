"""
Follow Builders 自建模式（方案 B）— 本地运行 feed 生成脚本

当 FOLLOW_BUILDERS_SELF_HOST=true 时，在每次 Pipeline 运行前
先执行 Node.js 脚本抓取最新的 Twitter/Podcast/Blog 数据，
生成 feed-*.json 到本地目录，然后 FollowBuildersSource 从本地读取。

依赖：
  - Node.js >= 20
  - npm install (在 scripts/ 目录下)
  - X_BEARER_TOKEN (Twitter API, Pay-per-use ~$4/月)
  - SUPADATA_API_KEY (YouTube 字幕 API, Basic $5/月)
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import config

logger = logging.getLogger(__name__)

FEED_SCRIPT_DIR = Path(config.BASE_DIR) / "social_media_sourcing" / "follow-builders" / "scripts"
FEED_OUTPUT_DIR = Path(config.BASE_DIR) / "social_media_sourcing" / "follow-builders"


async def run_feed_generator() -> bool:
    """
    运行 follow-builders 的 generate-feed.js 脚本。
    仅在 FOLLOW_BUILDERS_SELF_HOST=true 时调用。
    返回是否成功。
    """
    if not config.FOLLOW_BUILDERS_SELF_HOST:
        return False

    script_path = FEED_SCRIPT_DIR / "generate-feed.js"
    if not script_path.exists():
        logger.warning(
            f"[follow-builders-selfhost] 脚本不存在: {script_path}\n"
            f"请先 git clone 并 npm install:\n"
            f"  cd {FEED_SCRIPT_DIR} && npm install"
        )
        return False

    node_modules = FEED_SCRIPT_DIR / "node_modules"
    if not node_modules.exists():
        logger.warning(
            f"[follow-builders-selfhost] 依赖未安装，请先运行:\n"
            f"  cd {FEED_SCRIPT_DIR} && npm install"
        )
        return False

    env = os.environ.copy()

    x_token = config.FOLLOW_BUILDERS_X_BEARER_TOKEN
    supadata_key = config.FOLLOW_BUILDERS_SUPADATA_API_KEY

    if not x_token and not supadata_key:
        logger.warning(
            "[follow-builders-selfhost] X_BEARER_TOKEN 和 SUPADATA_API_KEY 均未配置，"
            "无法抓取任何内容"
        )
        return False

    flags = []
    if x_token:
        env["X_BEARER_TOKEN"] = x_token
    else:
        logger.info("[follow-builders-selfhost] X_BEARER_TOKEN 未配置，跳过 Twitter 抓取")

    if supadata_key:
        env["SUPADATA_API_KEY"] = supadata_key
    else:
        logger.info("[follow-builders-selfhost] SUPADATA_API_KEY 未配置，跳过播客抓取")

    if x_token and not supadata_key:
        flags.append("--tweets-only")
    elif supadata_key and not x_token:
        flags.append("--podcasts-only")

    cmd = ["node", str(script_path)] + flags
    logger.info(f"[follow-builders-selfhost] 运行: {' '.join(cmd)}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(FEED_SCRIPT_DIR),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode == 0:
            logger.info("[follow-builders-selfhost] Feed 生成成功")
            if stderr:
                for line in stderr.decode().strip().split("\n"):
                    if line.strip():
                        logger.debug(f"  {line}")
            return True
        else:
            logger.warning(
                f"[follow-builders-selfhost] Feed 生成失败 (exit={proc.returncode})\n"
                f"stderr: {stderr.decode()[:500]}"
            )
            return False

    except asyncio.TimeoutError:
        logger.warning("[follow-builders-selfhost] Feed 生成超时 (120s)")
        return False
    except FileNotFoundError:
        logger.warning(
            "[follow-builders-selfhost] 未找到 node 命令，请确保 Node.js >= 20 已安装"
        )
        return False
    except Exception as e:
        logger.warning(f"[follow-builders-selfhost] Feed 生成异常: {e}")
        return False
