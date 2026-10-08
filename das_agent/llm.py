"""구조화 계획 공급자.

- OllamaProvider : 로컬 전용 (기본). 데이터가 PC 밖으로 나가지 않는다. 프록시 환경변수 무시, localhost만 허용.
- ClaudeProvider : Claude 구독(Claude Code 로그인, `claude auth login`) 사용. API 키 불필요. **명시적으로 선택할 때만** 쓰며
                   현재 설정·데이터 기간·검토기준 값·사용자 요청문만 전송한다. 출고 원본 행과 집계 수치 표는 보내지 않는다.
- NullProvider   : 모델 없음. 규칙 파서만.
Anthropic API 키 직접 호출 경로는 없다 (DAS_LLM=anthropic 은 NullProvider).
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional, Protocol
from urllib.parse import urlparse
import requests

DEFAULT_OLLAMA_MODEL = "qwen3:30b-a3b"
DEFAULT_CLAUDE_MODEL = "sonnet"   # Claude Code 별칭 (sonnet / opus / fable). DAS_CLAUDE_MODEL 로 변경

class LLMProvider(Protocol):
    name: str
    model: str
    def available(self) -> bool: ...
    def plan(self, system: str, user: str, schema: dict) -> Optional[dict]: ...

@dataclass
class NullProvider:
    name: str = "none"
    model: str = "-"
    reason: str = "언어모델 미사용 · 정형 요청만 처리"
    def available(self):
        return False
    def plan(self, system, user, schema):
        return None

class OllamaProvider:
    name = "ollama"
    def __init__(self, model=None, host=None):
        self.model = model or os.getenv("DAS_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
        self.host = (host or "http://127.0.0.1:11434").rstrip("/")
        parsed = urlparse(self.host)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.path:
            raise ValueError("Ollama는 이 PC의 localhost 주소만 사용할 수 있습니다.")
        if "cloud" in self.model.lower():
            raise ValueError("클라우드 모델은 이 로컬 전용 앱에서 사용할 수 없습니다.")
        self.session = requests.Session()
        self.session.trust_env = False
        self.reason = ""
        self._ok = None
    def _request(self, method, path, payload=None, timeout=120):
        response = self.session.request(method, self.host + path, json=payload, timeout=(3, timeout), allow_redirects=False)
        if response.is_redirect:
            raise ValueError("Ollama의 외부 주소 리다이렉션을 허용하지 않습니다.")
        response.raise_for_status()
        return response.json()
    def available(self):
        if self._ok is not None:
            return self._ok
        try:
            names = {m["name"] for m in self._request("GET", "/api/tags", timeout=3).get("models", [])}
            exact = self.model if ":" in self.model else self.model + ":latest"
            if exact not in names:
                raise ValueError(f"대화 모델이 없습니다. ollama pull {self.model}")
            info = self._request("POST", "/api/show", {"model": self.model}, timeout=5)
            if info.get("remote_host") or info.get("remote_model"):
                raise ValueError("원격 모델은 사용할 수 없습니다.")
            if "completion" not in info.get("capabilities", []):
                raise ValueError("대화 생성 기능이 없는 모델입니다.")
            self._ok = True
        except (requests.RequestException, ValueError, KeyError) as e:
            self._ok = False
            self.reason = str(e).splitlines()[0][:200]
        return self._ok
    def plan(self, system, user, schema):
        if not self.available():
            raise ValueError(self.reason)
        response = self._request("POST", "/api/chat", {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": schema, "stream": False, "think": False, "keep_alive": "10m",
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 2400},
        })
        return json.loads(response["message"]["content"])

class ClaudeProvider:
    """Claude 구독 계정으로 `claude -p` (헤드리스)를 호출한다. API 키·요금 과금 없음, 구독 사용량에 포함.

    경량 호출: MCP·설정·도구·CLAUDE.md 전부 비활성 (--strict-mcp-config, --setting-sources "", --tools "", 임시 작업 폴더).
    구조화 출력은 --json-schema 로 받는다. 전송 내용은 system/user 프롬프트 문자열뿐이다.
    """
    name = "claude"
    timeout = 120

    def __init__(self, model=None, exe=None):
        self.model = model or os.getenv("DAS_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)
        self.exe = exe or shutil.which("claude")
        self.reason = ""
        self._ok = None
        self.account = ""

    def _env(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}
        env.pop("CLAUDE_CODE_SIMPLE", None)   # 설정 시 키체인 읽기를 건너뛰어 미로그인으로 보임
        return env

    def available(self):
        if self._ok is not None:
            return self._ok
        if not self.exe:
            self._ok, self.reason = False, "Claude Code CLI(claude)가 없습니다. npm install -g @anthropic-ai/claude-code 후 claude auth login"
            return False
        try:
            r = subprocess.run([self.exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                               timeout=20, env=self._env())
            info = json.loads(r.stdout or "{}")
            if not info.get("loggedIn"):
                raise ValueError("Claude 로그인이 필요합니다: 터미널에서 claude auth login")
            self.account = f"{info.get('authMethod', '')} {info.get('email', '')}".strip()
            self._ok = True
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            self._ok, self.reason = False, str(e).splitlines()[0][:200]
        return self._ok

    def plan(self, system, user, schema):
        if not self.available():
            raise ValueError(self.reason)
        cmd = [self.exe, "-p", "--output-format", "json", "--json-schema", json.dumps(schema, ensure_ascii=False),
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
               "--tools", "", "--max-turns", "1", "--model", self.model, "--system-prompt", system, user]
        with tempfile.TemporaryDirectory(prefix="das_claude_") as cwd:   # CLAUDE.md 자동 탐색 차단
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=self.timeout,
                               cwd=cwd, env=self._env())
        if r.returncode != 0 and not r.stdout.strip():
            raise ValueError(f"claude 실행 실패: {(r.stderr or '').strip()[:200]}")
        try:
            out = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise ValueError(f"claude 응답 해석 실패: {e}")
        if out.get("is_error"):
            raise ValueError(f"claude 오류: {str(out.get('result', ''))[:200]}")
        plan = out.get("structured_output")
        if plan is None:
            plan = json.loads(out.get("result") or "{}")
        return plan


def get_provider(pref=None):
    pref = (pref or os.getenv("DAS_LLM", "none")).lower()
    if pref == "none":
        return NullProvider()
    if pref == "claude":
        provider = ClaudeProvider()
        return provider if provider.available() else NullProvider(reason=provider.reason)
    if pref not in ("auto", "ollama"):
        return NullProvider(reason="지원하지 않는 모드입니다. 로컬 AI(Ollama) 또는 Claude 구독을 선택하세요.")
    try:
        provider = OllamaProvider()
        return provider if provider.available() else NullProvider(reason=provider.reason)
    except ValueError as e:
        return NullProvider(reason=str(e))
