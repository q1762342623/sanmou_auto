"""DeepSeek 客户端（零依赖，只用标准库 urllib）。

用途：给「自动战斗 Agent」提供决策大脑——把当前战场状态压成 JSON 发给
DeepSeek，让它选择下一条原子指令。**只发结构化状态，不发截图**
（截图/OCR 一律在指令内部完成，Agent 只看结论，省 token 也更稳）。

密钥来源（优先级从高到低）：
1. 环境变量 `DEEPSEEK_API_KEY`
2. 构造函数参数 `api_key`
3. 同目录 `secret.json`：`{"apiKey": "sk-..."}`（已 gitignore）

用法：
    from sanmou_auto.agent.deepseek import DeepSeekClient
    client = DeepSeekClient()
    out = client.json_chat([{"role": "user", "content": "..."}])
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
SECRET_PATH = Path(__file__).with_name("secret.json")


class DeepSeekError(RuntimeError):
    """调用失败（网络/鉴权/返回不可解析）。"""


class DeepSeekClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str = DEFAULT_MODEL, timeout: float = 60.0,
                 retries: int = 2, log=print):
        self.api_key = api_key
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL")
                         or DEFAULT_BASE).rstrip("/")
        self.model = os.environ.get("DEEPSEEK_MODEL") or model
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.log = log
        self.calls = 0
        self.failures = 0
        self.last_error = ""
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    # ------------------------------------------------------------------ 密钥
    def _key(self) -> str:
        key = (os.environ.get("DEEPSEEK_API_KEY") or self.api_key or "").strip()
        if key:
            return key
        try:
            data = json.loads(SECRET_PATH.read_text(encoding="utf-8-sig"))
            key = str(data.get("apiKey") or "").strip()
        except Exception:          # noqa: BLE001
            key = ""
        if not key:
            raise DeepSeekError(
                f"没有 DeepSeek 密钥：请设置 DEEPSEEK_API_KEY 或写入 {SECRET_PATH}")
        return key

    # ------------------------------------------------------------------ 请求
    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._key()}"})
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise DeepSeekError(f"返回不是 JSON：{raw[:200]}") from error

    def chat(self, messages: list[dict], *, temperature: float = 0.1,
             max_tokens: int = 500, json_mode: bool = True) -> str:
        """调一次 chat/completions，返回纯文本内容。"""
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        last = ""
        for attempt in range(self.retries + 1):
            try:
                self.calls += 1
                data = self._post(payload)
                usage = data.get("usage") or {}
                self.total_prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.total_completion_tokens += int(
                    usage.get("completion_tokens") or 0)
                choices = data.get("choices") or []
                if not choices:
                    raise DeepSeekError(f"没有 choices：{str(data)[:200]}")
                return str((choices[0].get("message") or {}).get("content") or "")
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                    DeepSeekError, OSError) as error:
                last = f"{type(error).__name__}: {error}"
                self.last_error = last
                self.log(f"  ! DeepSeek 调用失败（第 {attempt + 1} 次）：{last}")
                if attempt < self.retries:
                    time.sleep(min(8.0, 2.0 * (attempt + 1)))
        self.failures += 1
        raise DeepSeekError(last or "未知错误")

    def json_chat(self, messages: list[dict], **kwargs) -> dict:
        """要求模型输出 JSON 并解析（容忍 ```json 包裹 / 控制字符）。"""
        text = self.chat(messages, **kwargs).strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise DeepSeekError(f"模型没有返回 JSON：{text[:200]}")
        body = text[start:end + 1]
        for strict in (True, False):
            try:
                return json.loads(body, strict=strict)
            except json.JSONDecodeError:
                continue
        # 兜底：把裸换行/制表符等控制字符压掉再试
        cleaned = "".join(ch if ch >= " " or ch in "\n\r\t" else " "
                          for ch in body)
        try:
            return json.loads(cleaned, strict=False)
        except json.JSONDecodeError as error:
            raise DeepSeekError(f"JSON 解析失败：{body[:200]}") from error

    def stats(self) -> dict:
        return {
            "model": self.model,
            "calls": self.calls,
            "failures": self.failures,
            "promptTokens": self.total_prompt_tokens,
            "completionTokens": self.total_completion_tokens,
            "lastError": self.last_error,
        }
